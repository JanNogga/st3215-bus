// Fails early, with a clear message, when the NormaCore submodule that
// src/lib.rs compiles the protocol from is missing. Nothing else.
use std::path::Path;

const PROTOCOL: &str = "third_party/norma-core/software/drivers/st3215/src/protocol/mod.rs";

fn main() {
    println!("cargo:rerun-if-changed={PROTOCOL}");
    if !Path::new(PROTOCOL).is_file() {
        eprintln!(
            "error: {PROTOCOL} not found.\n\
             third_party/norma-core is missing: clone with --recurse-submodules \
             or run `git submodule update --init`."
        );
        std::process::exit(1);
    }
}
