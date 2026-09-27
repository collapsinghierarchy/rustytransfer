use rustytransfer_protocol::sender::ResumeOffer;
use sha3::{Digest, Sha3_256};
use std::{
    io,
    path::{Path, PathBuf},
};
use tokio::{
    fs::{File, OpenOptions},
    io::{AsyncReadExt, AsyncSeekExt},
};

fn no_follow(options: &mut OpenOptions) {
    #[cfg(unix)]
    options.custom_flags(libc::O_NOFOLLOW);
    #[cfg(windows)]
    options.custom_flags(windows_sys::Win32::Storage::FileSystem::FILE_FLAG_OPEN_REPARSE_POINT);
}

/// A same-directory partial output retained across connections and process runs.
pub(crate) struct ResumeOutput {
    path: PathBuf,
    file: Option<File>,
    candidate: ResumeOffer,
}

impl ResumeOutput {
    pub(crate) async fn open(destination: &Path) -> io::Result<Self> {
        if tokio::fs::try_exists(destination).await? {
            return Err(io::Error::new(
                io::ErrorKind::AlreadyExists,
                "destination already exists",
            ));
        }
        let name = destination.file_name().ok_or_else(|| {
            io::Error::new(io::ErrorKind::InvalidInput, "destination has no file name")
        })?;
        let mut part_name = std::ffi::OsString::from(".");
        part_name.push(name);
        part_name.push(".rustytransfer.part");
        let path = destination.with_file_name(part_name);

        let metadata = match tokio::fs::symlink_metadata(&path).await {
            Ok(metadata) => {
                if metadata.file_type().is_symlink() || !metadata.is_file() {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidInput,
                        "resume path must be a regular file, not a symlink",
                    ));
                }
                metadata
            }
            Err(error) if error.kind() == io::ErrorKind::NotFound => {
                let mut options = OpenOptions::new();
                options.read(true).write(true).create_new(true);
                no_follow(&mut options);
                let file = options.open(&path).await?;
                let std_file = file.into_std().await;
                std_file.try_lock().map_err(|error| {
                    io::Error::new(io::ErrorKind::WouldBlock, error.to_string())
                })?;
                let file = File::from_std(std_file);
                return Self::hash_candidate(path, file).await;
            }
            Err(error) => return Err(error),
        };
        if !metadata.is_file() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "resume path is not a regular file",
            ));
        }

        let mut options = OpenOptions::new();
        options.read(true).write(true);
        no_follow(&mut options);
        let file = options.open(&path).await?;
        let std_file = file.into_std().await;
        std_file
            .try_lock()
            .map_err(|error| io::Error::new(io::ErrorKind::WouldBlock, error.to_string()))?;
        if !std_file.metadata()?.is_file() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "resume path is not a regular file",
            ));
        }
        let path_metadata = tokio::fs::symlink_metadata(&path).await?;
        if path_metadata.file_type().is_symlink() || !path_metadata.is_file() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "resume path changed to a symlink or non-regular file",
            ));
        }
        Self::hash_candidate(path, File::from_std(std_file)).await
    }

    async fn hash_candidate(path: PathBuf, mut file: File) -> io::Result<Self> {
        let length = file.metadata().await?.len();
        file.seek(std::io::SeekFrom::Start(0)).await?;
        let mut hasher = Sha3_256::new();
        let mut buffer = vec![0_u8; 64 * 1024];
        let buffer_len = u64::try_from(buffer.len())
            .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error.to_string()))?;
        let mut remaining = length;
        let mut hashed = 0_u64;
        while remaining > 0 {
            let count = usize::try_from(remaining.min(buffer_len))
                .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error.to_string()))?;
            let read_buffer = buffer.get_mut(..count).ok_or_else(|| {
                io::Error::new(
                    io::ErrorKind::InvalidData,
                    "resume hash buffer range is invalid",
                )
            })?;
            let read = file.read(read_buffer).await?;
            if read == 0 {
                break;
            }
            let hashed_bytes = buffer.get(..read).ok_or_else(|| {
                io::Error::new(
                    io::ErrorKind::InvalidData,
                    "resume hash read range is invalid",
                )
            })?;
            hasher.update(hashed_bytes);
            let read = u64::try_from(read)
                .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error.to_string()))?;
            hashed = hashed.checked_add(read).ok_or_else(|| {
                io::Error::new(io::ErrorKind::InvalidData, "resume prefix length overflow")
            })?;
            remaining = remaining.saturating_sub(read);
        }
        file.seek(std::io::SeekFrom::Start(0)).await?;
        Ok(Self {
            path,
            file: Some(file),
            candidate: ResumeOffer {
                offset: hashed,
                prefix_digest: hasher.finalize().into(),
            },
        })
    }

    pub(crate) const fn candidate(&self) -> ResumeOffer {
        self.candidate
    }

    pub(crate) async fn select(&mut self, offset: u64, digest: [u8; 32]) -> io::Result<()> {
        let empty_digest: [u8; 32] = Sha3_256::digest([]).into();
        let accepted_candidate =
            offset == self.candidate.offset && digest == self.candidate.prefix_digest;
        let selected_reset = offset == 0 && digest == empty_digest;
        if !accepted_candidate && !selected_reset {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "authenticated resume selection does not match the partial file",
            ));
        }
        let file = self.file.as_mut().ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::BrokenPipe,
                "resume output is already committed",
            )
        })?;
        file.set_len(offset).await?;
        file.seek(std::io::SeekFrom::Start(offset)).await?;
        Ok(())
    }

    pub(crate) async fn write_all(&mut self, bytes: &[u8]) -> io::Result<()> {
        let file = self.file.as_mut().ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::BrokenPipe,
                "resume output is already committed",
            )
        })?;
        tokio::io::AsyncWriteExt::write_all(file, bytes).await
    }

    pub(crate) async fn flush(&mut self) -> io::Result<()> {
        let file = self.file.as_mut().ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::BrokenPipe,
                "resume output is already committed",
            )
        })?;
        tokio::io::AsyncWriteExt::flush(file).await
    }

    pub(crate) async fn sync_data(&mut self) -> io::Result<()> {
        let file = self.file.as_mut().ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::BrokenPipe,
                "resume output is already committed",
            )
        })?;
        file.sync_data().await
    }

    pub(crate) async fn commit(&mut self, destination: &Path) -> io::Result<()> {
        self.sync_data().await?;
        tokio::fs::hard_link(&self.path, destination).await?;
        drop(self.file.take());
        match tokio::fs::remove_file(&self.path).await {
            Ok(()) => Ok(()),
            Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
            Err(error) => {
                eprintln!(
                    "warning: committed output, but could not remove partial file {}: {error}",
                    self.path.display()
                );
                Ok(())
            }
        }
    }
}
