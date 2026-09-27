use std::{
    io,
    path::{Path, PathBuf},
    sync::atomic::{AtomicU64, Ordering},
};
use tokio::fs::{File, OpenOptions};

static NEXT_TEMP_FILE: AtomicU64 = AtomicU64::new(0);

pub(crate) struct TempOutput {
    path: Option<PathBuf>,
}

impl TempOutput {
    pub(crate) const fn new() -> Self {
        Self { path: None }
    }

    pub(crate) async fn create(&mut self, destination: &Path) -> io::Result<File> {
        if tokio::fs::try_exists(destination).await? {
            return Err(io::Error::new(
                io::ErrorKind::AlreadyExists,
                "destination already exists",
            ));
        }
        let name = destination.file_name().ok_or_else(|| {
            io::Error::new(io::ErrorKind::InvalidInput, "destination has no file name")
        })?;
        for _ in 0..16 {
            let sequence = NEXT_TEMP_FILE.fetch_add(1, Ordering::Relaxed);
            let mut temp_name = std::ffi::OsString::from(".");
            temp_name.push(name);
            temp_name.push(format!(
                ".rustytransfer-{}-{sequence}.part",
                std::process::id()
            ));
            let path = destination.with_file_name(temp_name);
            match OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(&path)
                .await
            {
                Ok(file) => {
                    self.path = Some(path);
                    return Ok(file);
                }
                Err(error) if error.kind() == io::ErrorKind::AlreadyExists => continue,
                Err(error) => return Err(error),
            }
        }
        Err(io::Error::new(
            io::ErrorKind::AlreadyExists,
            "could not reserve a temporary output file",
        ))
    }

    pub(crate) async fn commit(&mut self, destination: &Path) -> io::Result<()> {
        let path = self.path.as_ref().ok_or_else(|| {
            io::Error::new(io::ErrorKind::NotFound, "temporary output file is missing")
        })?;
        // The temporary file is in the same directory. A hard link publishes it
        // atomically and fails when the destination already exists.
        tokio::fs::hard_link(path, destination).await?;
        match tokio::fs::remove_file(path).await {
            Ok(()) => self.path = None,
            Err(error) => eprintln!(
                "warning: committed file, but could not remove temporary copy {}: {error}",
                path.display()
            ),
        }
        Ok(())
    }

    pub(crate) async fn cleanup(&mut self) -> io::Result<()> {
        if let Some(path) = self.path.as_ref() {
            match tokio::fs::remove_file(path).await {
                Ok(()) => {
                    self.path = None;
                    Ok(())
                }
                Err(error) if error.kind() == io::ErrorKind::NotFound => {
                    self.path = None;
                    Ok(())
                }
                Err(error) => Err(error),
            }
        } else {
            Ok(())
        }
    }
}

impl Drop for TempOutput {
    fn drop(&mut self) {
        if let Some(path) = self.path.take() {
            let _cleanup_result = std::fs::remove_file(path);
        }
    }
}
