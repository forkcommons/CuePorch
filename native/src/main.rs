#![cfg_attr(target_os = "windows", windows_subsystem = "windows")]
use std::{
    env, fs,
    io::{self, BufRead, BufReader},
    path::{Path, PathBuf},
    process::{Command, Stdio},
};

fn profile() -> io::Result<PathBuf> {
    let var = if cfg!(windows) {
        "LOCALAPPDATA"
    } else {
        "HOME"
    };
    let base = PathBuf::from(
        env::var_os(var).ok_or_else(|| io::Error::other("User profile unavailable"))?,
    );
    let path = if cfg!(windows) {
        base.join("ForkCommons/CuePorch/Data")
    } else if cfg!(target_os = "macos") {
        base.join("Library/Application Support/ForkCommons/CuePorch")
    } else {
        env::var_os("XDG_DATA_HOME")
            .map(PathBuf::from)
            .unwrap_or(base.join(".local/share"))
            .join("cueporch")
    };
    Ok(path)
}

fn backend(root: &Path) -> Command {
    let path = root.join("service").join(if cfg!(windows) {
        "cueporch-service.exe"
    } else {
        "cueporch-service"
    });
    let mut command = Command::new(path);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000);
    }
    command
}

fn run() -> io::Result<()> {
    let exe = env::current_exe()?;
    let root = exe
        .parent()
        .ok_or_else(|| io::Error::other("Installation folder unavailable"))?;
    let mut args = env::args_os().skip(1).peekable();
    if args.peek().is_some_and(|arg| arg == "--dash-transmitter") {
        let status = backend(root).args(args).status()?;
        return if status.success() {
            Ok(())
        } else {
            Err(io::Error::other(
                "The MPEG-DASH transmitter stopped with an error",
            ))
        };
    }
    let mut home = profile()?;
    let mut browser = true;
    while let Some(arg) = args.next() {
        if arg == "--data" {
            home = PathBuf::from(
                args.next()
                    .ok_or_else(|| io::Error::other("Missing data folder"))?,
            );
        } else if arg == "--headless" {
            browser = false;
        } else {
            return Err(io::Error::other("Unknown launcher option"));
        }
    }
    fs::create_dir_all(&home)?;
    let log = fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(home.join("service.log"))?;
    let mut child = backend(root)
        .arg("--data")
        .arg(&home)
        .stdout(Stdio::piped())
        .stderr(log)
        .spawn()?;
    let mut ready = String::new();
    BufReader::new(child.stdout.take().unwrap()).read_line(&mut ready)?;
    let url = ready.trim();
    if !url.starts_with("http://127.0.0.1:") || !url.contains("/desktop#") {
        let _ = child.kill();
        let _ = child.wait();
        return Err(io::Error::other(
            "Could not start CuePorch. See service.log in your data folder.",
        ));
    }
    if browser {
        let mut open = if cfg!(windows) {
            let mut c = Command::new("rundll32.exe");
            c.arg("url.dll,FileProtocolHandler");
            c
        } else if cfg!(target_os = "macos") {
            Command::new("open")
        } else {
            Command::new("xdg-open")
        };
        open.arg(url).spawn()?;
    }
    if !child.wait()?.success() {
        return Err(io::Error::other(
            "CuePorch exited unexpectedly. See service.log.",
        ));
    }
    Ok(())
}
fn main() {
    if let Err(error) = run() {
        eprintln!("{error}");
        #[cfg(windows)]
        {
            #[link(name = "user32")]
            extern "system" {
                fn MessageBoxW(hwnd: usize, text: *const u16, title: *const u16, flags: u32)
                    -> i32;
            }
            if !env::args_os().any(|a| a == "--headless" || a == "--dash-transmitter") {
                let msg: Vec<u16> = error.to_string().encode_utf16().chain(Some(0)).collect();
                let title: Vec<u16> = "CuePorch".encode_utf16().chain(Some(0)).collect();
                unsafe {
                    MessageBoxW(0, msg.as_ptr(), title.as_ptr(), 16);
                }
            }
        }
        std::process::exit(1)
    }
}
