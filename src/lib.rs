//! Python bindings for Feetech ST3215 servo buses, built in Rust on NormaCore's
//! st3215 protocol.
//!
//! Every byte on the wire is encoded and decoded by NormaCore's `st3215`
//! protocol module, compiled unchanged as [`protocol`]. This crate is a thin
//! layer on top of it: it chooses which requests to send and hands the results
//! to Python.
//!
//! # Why this is a Rust extension rather than Python
//!
//! NormaCore's protocol module offers no sync-*read* request: `ST3215Request`
//! has `SyncWrite` (0x83) for writing many motors in one packet, but reading is
//! one request/response round trip per motor. Two arms with leaders are 4 buses
//! x 8 motors = 32 round trips per control cycle, and at 30 Hz there are 33 ms
//! to do them in. Driving that from Python would pay the interpreter's overhead
//! 32 times per cycle and serialise the buses behind the GIL.
//!
//! So the batching lives here: one call reads every motor on every bus, with
//! the buses driven concurrently on a tokio runtime, and Python pays for one
//! call per cycle instead of 32. The GIL is released for the duration.
//!
//! ```python
//! import st3215_bus
//! buses = st3215_bus.Buses({"arm": "/dev/ttyUSB0"})
//! buses.read_positions()            # {"arm": {1: 2047, ...}}
//! buses.write_goals({"arm": {1: 2100}})
//! ```

// NormaCore's st3215 protocol, compiled straight from the NormaCore repository
// pinned as a git submodule at third_party/norma-core (see build.rs). Only this
// module is used: NormaCore's st3215 crate as a whole carries station
// dependencies this package does not need.
#[path = "../third_party/norma-core/software/drivers/st3215/src/protocol/mod.rs"]
pub mod protocol;

use std::collections::HashMap;
use std::sync::Arc;

use bytes::Bytes;
use pyo3::exceptions::{PyIOError, PyRuntimeError};
use pyo3::prelude::*;
use crate::protocol::{EepromRegister, RamRegister, ST3215Request, ST3215Response, SUPPORTED_BAUD_RATES};
use tokio::sync::Mutex;
use tokio_serial::{SerialPortBuilderExt, SerialStream};

/// Per-request timeout. Steady-state replies at 1 Mbaud come back well inside a
/// millisecond; this only bounds how long a missing motor costs us.
const DEFAULT_TIMEOUT_MS: u64 = 20;

/// The first ping after a bus powers up is often missed while the servos settle
/// -- observed on real hardware -- so a ping is retried before an empty bus is
/// believed.
const PING_ATTEMPTS: u32 = 3;

/// Attempts per register read.
///
/// Measured on real hardware: reading 4 buses x 8 motors takes 2.8 ms against
/// a 33.3 ms budget at 30 Hz, and about 0.2% of cycles had exactly one motor
/// fail to answer, mostly on a bus powered only enough for its servos to be
/// read. A single immediate retry costs one extra round trip on the rare bad
/// read, which the headroom absorbs easily, and turns a dropped joint reading
/// into a slightly later one.
const READ_ATTEMPTS: u32 = 2;

struct Bus {
    name: String,
    port: Mutex<SerialStream>,
}

async fn read_u16(port: &mut SerialStream, motor: u8, reg: RamRegister, timeout_ms: u64) -> Option<u16> {
    let request = ST3215Request::Read {
        motor,
        address: reg.address(),
        length: reg.size(),
    };
    match request.async_readwrite(port, timeout_ms).await {
        Ok(ST3215Response::Read { data, .. }) => match reg.size() {
            1 => data.first().map(|b| *b as u16),
            _ => (data.len() >= 2).then(|| data[0] as u16 | ((data[1] as u16) << 8)),
        },
        _ => None,
    }
}

async fn read_eeprom_u16(
    port: &mut SerialStream,
    motor: u8,
    reg: EepromRegister,
    timeout_ms: u64,
) -> Option<u16> {
    let request = ST3215Request::Read {
        motor,
        address: reg.address(),
        length: reg.size(),
    };
    match request.async_readwrite(port, timeout_ms).await {
        Ok(ST3215Response::Read { data, .. }) => match reg.size() {
            1 => data.first().map(|b| *b as u16),
            _ => (data.len() >= 2).then(|| data[0] as u16 | ((data[1] as u16) << 8)),
        },
        _ => None,
    }
}

/// Write one EEPROM register on one motor: unlock, RegWrite + Action, verify,
/// re-lock. Retries, because a single attempt is not reliable.
///
/// EEPROM is locked in normal operation, so a bare write is silently ignored --
/// nothing reports it and the old value simply stays. It also needs the
/// RegWrite + Action pair rather than a plain Write: a direct write was tried
/// first and landed on roughly a third of the motors, which is worse than
/// failing outright because the arm is then half-configured. This mirrors
/// `reg_write_with_action` in NormaCore's st3215 driver, including the
/// read-back as part of the write and the retry count.
async fn write_eeprom_u16(
    port: &mut SerialStream,
    motor: u8,
    reg: EepromRegister,
    value: u16,
    timeout_ms: u64,
) -> bool {
    const MAX_RETRIES: u8 = 5;
    let data: Vec<u8> = match reg.size() {
        1 => vec![value as u8],
        _ => vec![(value & 0xFF) as u8, (value >> 8) as u8],
    };

    let unlock = ST3215Request::Write {
        motor,
        address: RamRegister::Lock.address(),
        data: Bytes::from_static(&[0]),
    };
    if unlock.async_readwrite(port, timeout_ms).await.is_err() {
        return false;
    }

    let mut ok = false;
    for _ in 0..MAX_RETRIES {
        let reg_write = ST3215Request::RegWrite {
            motor,
            address: reg.address(),
            data: Bytes::from(data.clone()),
        };
        if reg_write.async_readwrite(port, timeout_ms).await.is_err() {
            continue;
        }
        // Bound first: in an `if` condition Rust reads `Action { motor }` as a
        // block rather than a struct literal.
        let action = ST3215Request::Action { motor };
        if action.async_readwrite(port, timeout_ms).await.is_err() {
            continue;
        }
        if read_eeprom_u16(port, motor, reg, timeout_ms).await == Some(value) {
            ok = true;
            break;
        }
    }

    // Re-lock whatever happened: leaving EEPROM writable is worse than a
    // failed write, because the next stray packet could land in it.
    let relock = ST3215Request::Write {
        motor,
        address: RamRegister::Lock.address(),
        data: Bytes::from_static(&[1]),
    };
    let _ = relock.async_readwrite(port, timeout_ms).await;
    ok
}

async fn ping_once(port: &mut SerialStream, motor: u8, timeout_ms: u64) -> bool {
    ST3215Request::Ping { motor }
        .async_readwrite(port, timeout_ms)
        .await
        .is_ok()
}

/// Buses to a set of ST3215 adapters, addressed by role name.
#[pyclass(module = "st3215_bus")]
struct Buses {
    rt: tokio::runtime::Runtime,
    buses: Vec<Arc<Bus>>,
    timeout_ms: u64,
}

#[pymethods]
impl Buses {
    /// `ports` maps a role name to a device path, e.g.
    /// `{"arm": "/dev/ttyUSB0"}`.
    #[new]
    #[pyo3(signature = (ports, timeout_ms = DEFAULT_TIMEOUT_MS), text_signature = "(ports, timeout_ms=20)")]
    fn new(ports: HashMap<String, String>, timeout_ms: u64) -> PyResult<Self> {
        let rt = tokio::runtime::Builder::new_multi_thread()
            .enable_all()
            .build()
            .map_err(|e| PyRuntimeError::new_err(format!("tokio runtime: {e}")))?;

        // Sorted so the order Python sees is stable rather than hash order.
        let mut items: Vec<_> = ports.into_iter().collect();
        items.sort();

        // Opening MUST happen inside the runtime: open_native_async registers
        // the port with the tokio reactor, and outside a runtime context that
        // panics with "there is no reactor running".
        let buses = rt.block_on(async {
            let mut opened = Vec::with_capacity(items.len());
            for (name, path) in items {
                let stream = tokio_serial::new(&path, SUPPORTED_BAUD_RATES[0])
                    .open_native_async()
                    .map_err(|e| {
                        PyIOError::new_err(format!("could not open {path} for {name}: {e}"))
                    })?;
                opened.push(Arc::new(Bus {
                    name,
                    port: Mutex::new(stream),
                }));
            }
            Ok::<_, PyErr>(opened)
        })?;

        Ok(Self { rt, buses, timeout_ms })
    }

    /// Which motor ids answer, per bus. Retries, because a bus whose PSU has
    /// just come up commonly misses the first ping.
    #[pyo3(signature = (highest_id = 8))]
    fn ping(&self, py: Python<'_>, highest_id: u8) -> HashMap<String, Vec<u8>> {
        let timeout = self.timeout_ms;
        let buses = self.buses.clone();
        py.allow_threads(|| {
            self.rt.block_on(async move {
                let tasks = buses.into_iter().map(|bus| async move {
                    let mut port = bus.port.lock().await;
                    let mut found = Vec::new();
                    for motor in 1..=highest_id {
                        for attempt in 0..PING_ATTEMPTS {
                            if ping_once(&mut port, motor, timeout).await {
                                found.push(motor);
                                break;
                            }
                            if attempt + 1 < PING_ATTEMPTS {
                                tokio::time::sleep(std::time::Duration::from_millis(60)).await;
                            }
                        }
                    }
                    (bus.name.clone(), found)
                });
                futures::future::join_all(tasks).await.into_iter().collect()
            })
        })
    }

    /// Present position of each motor, per bus, in encoder counts (4096/turn).
    ///
    /// All buses are read concurrently; motors within a bus are sequential
    /// because the protocol module offers no sync-read request. A motor that
    /// does not answer is omitted rather than reported as zero -- zero is a
    /// legal position.
    #[pyo3(
        signature = (motors = vec![1, 2, 3, 4, 5, 6, 7, 8]),
        text_signature = "($self, motors=[1, 2, 3, 4, 5, 6, 7, 8])"
    )]
    fn read_positions(&self, py: Python<'_>, motors: Vec<u8>) -> HashMap<String, HashMap<u8, u16>> {
        self.read_register_all(py, RamRegister::PresentPosition, motors)
    }

    /// As `read_positions`, for any RAM register named as in the protocol's
    /// `RamRegister` (e.g. "PresentCurrent", "PresentLoad", "TorqueEnable").
    fn read_register(
        &self,
        py: Python<'_>,
        register: &str,
        motors: Vec<u8>,
    ) -> PyResult<HashMap<String, HashMap<u8, u16>>> {
        let reg = register_by_name(register)
            .ok_or_else(|| PyRuntimeError::new_err(format!("unknown RAM register: {register}")))?;
        Ok(self.read_register_all(py, reg, motors))
    }

    /// Command goal positions. One `SyncWrite` packet per bus, buses in
    /// parallel -- the whole point of the protocol having a bulk write.
    fn write_goals(&self, py: Python<'_>, goals: HashMap<String, HashMap<u8, u16>>) -> PyResult<()> {
        self.sync_write(py, RamRegister::GoalPosition, goals, RamRegister::GoalPosition.size() as usize)
    }

    /// Write any RAM register on the listed motors, per bus.
    ///
    /// The width comes from the register itself rather than an argument, so a
    /// 1-byte register cannot be sent as 2 by mistake.
    fn write_register(
        &self,
        py: Python<'_>,
        register: &str,
        values: HashMap<String, HashMap<u8, u16>>,
    ) -> PyResult<()> {
        let reg = register_by_name(register)
            .ok_or_else(|| PyRuntimeError::new_err(format!("unknown RAM register: {register}")))?;
        let width = reg.size() as usize;
        self.sync_write(py, reg, values, width)
    }

    /// Read an EEPROM register, named as in the protocol's `EepromRegister`
    /// (e.g. "MaxTorque", "PCoef", "Offset"). Same shape as `read_register`.
    fn read_eeprom(
        &self,
        py: Python<'_>,
        register: &str,
        motors: Vec<u8>,
    ) -> PyResult<HashMap<String, HashMap<u8, u16>>> {
        let reg = eeprom_by_name(register)
            .ok_or_else(|| PyRuntimeError::new_err(format!("unknown EEPROM register: {register}")))?;
        let timeout = self.timeout_ms;
        let buses = self.buses.clone();
        Ok(py.allow_threads(|| {
            self.rt.block_on(async move {
                let tasks = buses.into_iter().map(|bus| {
                    let motors = motors.clone();
                    async move {
                        let mut port = bus.port.lock().await;
                        let mut out = HashMap::new();
                        for motor in motors {
                            for _ in 0..READ_ATTEMPTS {
                                if let Some(v) = read_eeprom_u16(&mut port, motor, reg, timeout).await {
                                    out.insert(motor, v);
                                    break;
                                }
                            }
                        }
                        (bus.name.clone(), out)
                    }
                });
                futures::future::join_all(tasks).await.into_iter().collect()
            })
        }))
    }

    /// Write an EEPROM register, per bus. Returns, for every bus held, the
    /// motors that did NOT take the value -- all lists empty means every write
    /// was verified.
    ///
    /// One motor at a time and read back each one: EEPROM is locked in normal
    /// operation, so an unverified write can be silently discarded.
    fn write_eeprom(
        &self,
        py: Python<'_>,
        register: &str,
        values: HashMap<String, HashMap<u8, u16>>,
    ) -> PyResult<HashMap<String, Vec<u8>>> {
        let reg = eeprom_by_name(register)
            .ok_or_else(|| PyRuntimeError::new_err(format!("unknown EEPROM register: {register}")))?;
        let timeout = self.timeout_ms;
        let buses = self.buses.clone();
        let known: Vec<String> = buses.iter().map(|b| b.name.clone()).collect();
        for name in values.keys() {
            if !known.contains(name) {
                return Err(PyRuntimeError::new_err(format!(
                    "no such bus: {name} (have {known:?})"
                )));
            }
        }
        Ok(py.allow_threads(|| {
            self.rt.block_on(async move {
                let tasks = buses.into_iter().map(|bus| {
                    let wanted = values.get(&bus.name).cloned().unwrap_or_default();
                    async move {
                        let mut port = bus.port.lock().await;
                        let mut failed = Vec::new();
                        for (motor, value) in wanted {
                            if !write_eeprom_u16(&mut port, motor, reg, value, timeout).await {
                                failed.push(motor);
                            }
                        }
                        failed.sort_unstable();
                        (bus.name.clone(), failed)
                    }
                });
                futures::future::join_all(tasks).await.into_iter().collect()
            })
        }))
    }

    /// Torque on or off for the listed motors, on every bus held.
    #[pyo3(
        signature = (enable, motors = vec![1, 2, 3, 4, 5, 6, 7, 8]),
        text_signature = "($self, enable, motors=[1, 2, 3, 4, 5, 6, 7, 8])"
    )]
    fn set_torque(&self, py: Python<'_>, enable: bool, motors: Vec<u8>) -> PyResult<()> {
        let value = u16::from(enable);
        let goals = self
            .buses
            .iter()
            .map(|b| {
                (
                    b.name.clone(),
                    motors.iter().map(|m| (*m, value)).collect::<HashMap<_, _>>(),
                )
            })
            .collect();
        self.sync_write(py, RamRegister::TorqueEnable, goals, RamRegister::TorqueEnable.size() as usize)
    }

    #[getter]
    fn names(&self) -> Vec<String> {
        self.buses.iter().map(|b| b.name.clone()).collect()
    }

    /// Release the serial ports.
    ///
    /// Worth calling explicitly rather than leaving to the garbage collector: a
    /// held port makes the next process fail with "Device or resource busy",
    /// and a script that exits untidily otherwise locks the bus out until its
    /// process really dies.
    fn close(&mut self) {
        self.buses.clear();
    }

    fn __enter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }

    fn __exit__(&mut self, _t: PyObject, _v: PyObject, _tb: PyObject) -> bool {
        self.close();
        false
    }

    fn __repr__(&self) -> String {
        format!("Buses({:?}, timeout_ms={})", self.names(), self.timeout_ms)
    }
}

impl Buses {
    fn read_register_all(
        &self,
        py: Python<'_>,
        reg: RamRegister,
        motors: Vec<u8>,
    ) -> HashMap<String, HashMap<u8, u16>> {
        let timeout = self.timeout_ms;
        let buses = self.buses.clone();
        py.allow_threads(|| {
            self.rt.block_on(async move {
                let tasks = buses.into_iter().map(|bus| {
                    let motors = motors.clone();
                    async move {
                        let mut port = bus.port.lock().await;
                        let mut out = HashMap::new();
                        for motor in motors {
                            for _ in 0..READ_ATTEMPTS {
                                if let Some(v) = read_u16(&mut port, motor, reg, timeout).await {
                                    out.insert(motor, v);
                                    break;
                                }
                            }
                        }
                        (bus.name.clone(), out)
                    }
                });
                futures::future::join_all(tasks).await.into_iter().collect()
            })
        })
    }

    fn sync_write(
        &self,
        py: Python<'_>,
        reg: RamRegister,
        values: HashMap<String, HashMap<u8, u16>>,
        width: usize,
    ) -> PyResult<()> {
        let timeout = self.timeout_ms;
        let buses = self.buses.clone();
        let known: Vec<String> = buses.iter().map(|b| b.name.clone()).collect();
        for name in values.keys() {
            if !known.contains(name) {
                return Err(PyRuntimeError::new_err(format!(
                    "no such bus: {name} (have {known:?})"
                )));
            }
        }
        py.allow_threads(|| {
            self.rt.block_on(async move {
                let tasks = buses.into_iter().map(|bus| {
                    let per_bus = values.get(&bus.name).cloned().unwrap_or_default();
                    async move {
                        if per_bus.is_empty() {
                            return Ok(());
                        }
                        // Sorted so the packet is deterministic run to run.
                        let mut pairs: Vec<_> = per_bus.into_iter().collect();
                        pairs.sort_by_key(|(motor, _)| *motor);
                        let data: Vec<(u8, Bytes)> = pairs
                            .into_iter()
                            .map(|(motor, value)| {
                                let bytes = if width == 1 {
                                    Bytes::from(vec![value as u8])
                                } else {
                                    Bytes::from(vec![value as u8, (value >> 8) as u8])
                                };
                                (motor, bytes)
                            })
                            .collect();
                        let mut port = bus.port.lock().await;
                        ST3215Request::SyncWrite {
                            address: reg.address(),
                            data,
                        }
                        .async_write(&mut *port, timeout)
                        .await
                        .map_err(|e| {
                            PyRuntimeError::new_err(format!("sync write on {}: {e:?}", bus.name))
                        })
                    }
                });
                for result in futures::future::join_all(tasks).await {
                    result?;
                }
                Ok(())
            })
        })
    }
}

fn register_by_name(name: &str) -> Option<RamRegister> {
    Some(match name {
        "TorqueEnable" => RamRegister::TorqueEnable,
        "Acc" => RamRegister::Acc,
        "GoalPosition" => RamRegister::GoalPosition,
        "GoalTime" => RamRegister::GoalTime,
        "GoalSpeed" => RamRegister::GoalSpeed,
        "TorqueLimit" => RamRegister::TorqueLimit,
        "Lock" => RamRegister::Lock,
        "PresentPosition" => RamRegister::PresentPosition,
        "PresentSpeed" => RamRegister::PresentSpeed,
        "PresentLoad" => RamRegister::PresentLoad,
        "PresentVoltage" => RamRegister::PresentVoltage,
        "PresentTemperature" => RamRegister::PresentTemperature,
        "Status" => RamRegister::Status,
        "Moving" => RamRegister::Moving,
        "PresentCurrent" => RamRegister::PresentCurrent,
        _ => return None,
    })
}

fn eeprom_by_name(name: &str) -> Option<EepromRegister> {
    Some(match name {
        "ModelNumber" => EepromRegister::ModelNumber,
        "FirmwareVersion" => EepromRegister::FirmwareVersion,
        "MaxTorque" => EepromRegister::MaxTorque,
        "MaxAngleLimit" => EepromRegister::MaxAngleLimit,
        "MinAngleLimit" => EepromRegister::MinAngleLimit,
        "ProtectionCurrent" => EepromRegister::ProtectionCurrent,
        "ProtectionTorque" => EepromRegister::ProtectionTorque,
        "OverloadTorque" => EepromRegister::OverloadTorque,
        "AngularResolution" => EepromRegister::AngularResolution,
        "MaxVoltage" => EepromRegister::MaxVoltage,
        "MinVoltage" => EepromRegister::MinVoltage,
        "UnloadCondition" => EepromRegister::UnloadCondition,
        "LedAlarm" => EepromRegister::LedAlarm,
        "PCoef" => EepromRegister::PCoef,
        "ICoef" => EepromRegister::ICoef,
        "DCoef" => EepromRegister::DCoef,
        "Offset" => EepromRegister::Offset,
        "Mode" => EepromRegister::Mode,
        "ID" => EepromRegister::ID,
        _ => return None,
    })
}

/// Python bindings for Feetech ST3215 servo buses, built in Rust on
/// NormaCore's st3215 protocol.
#[pymodule]
fn st3215_bus(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<Buses>()?;
    m.add("COUNTS_PER_TURN", 4096u32)?;
    Ok(())
}
