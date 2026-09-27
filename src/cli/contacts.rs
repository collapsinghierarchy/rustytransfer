use anyhow::{Context, Result, bail, ensure};
use serde::{Deserialize, Serialize};
use std::{
    collections::BTreeMap,
    fs::{self, OpenOptions},
    io::Write,
    path::PathBuf,
};

use super::{ContactCommand, DirectInvite, default_identity_path};

#[derive(Serialize, Deserialize)]
struct ContactFile {
    version: u32,
    contacts: BTreeMap<String, String>,
}

impl Default for ContactFile {
    fn default() -> Self {
        Self {
            version: 1,
            contacts: BTreeMap::new(),
        }
    }
}

fn contact_file_path() -> Result<PathBuf> {
    Ok(default_identity_path()?.with_file_name("contacts.json"))
}

fn load_contacts(path: &std::path::Path) -> Result<ContactFile> {
    let bytes = match fs::read(path) {
        Ok(bytes) => bytes,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return Ok(ContactFile::default());
        }
        Err(error) => return Err(error).with_context(|| format!("cannot read {}", path.display())),
    };
    let contacts: ContactFile = serde_json::from_slice(&bytes)
        .with_context(|| format!("invalid contact file {}", path.display()))?;
    ensure!(contacts.version == 1, "unsupported contacts file version");
    Ok(contacts)
}

fn save_contacts(path: &std::path::Path, contacts: &ContactFile) -> Result<()> {
    let parent = path
        .parent()
        .context("contacts file has no parent directory")?;
    fs::create_dir_all(parent)?;
    let temporary_path = path.with_extension(format!("json.{}.tmp", std::process::id()));
    let bytes = serde_json::to_vec_pretty(contacts)?;
    let mut options = OpenOptions::new();
    options.write(true).create(true).truncate(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let mut temporary = options
        .open(&temporary_path)
        .with_context(|| format!("cannot create {}", temporary_path.display()))?;
    temporary.write_all(&bytes)?;
    temporary.sync_all()?;
    fs::rename(&temporary_path, path).with_context(|| {
        format!(
            "cannot replace contacts file {} with {}",
            path.display(),
            temporary_path.display()
        )
    })?;
    Ok(())
}

fn validate_name(name: &str) -> Result<()> {
    ensure!(
        !name.is_empty()
            && name.len() <= 32
            && name
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || b"._-".contains(&byte)),
        "contact name must be 1-32 characters using letters, digits, '.', '_' or '-'"
    );
    Ok(())
}

fn endpoint_id(input: &str) -> Result<String> {
    let id = if input.starts_with("rt1:") {
        DirectInvite::parse(input)?.sender_id
    } else {
        input.to_owned()
    };
    crate::transport::iroh::validate_endpoint_id(&id)?;
    Ok(id)
}

pub(super) fn run(command: ContactCommand) -> Result<()> {
    let path = contact_file_path()?;
    let mut file = load_contacts(&path)?;
    match command {
        ContactCommand::List => {
            if file.contacts.is_empty() {
                println!(
                    "No contacts saved. Add one with: rustytransfer contacts add <name> <endpoint-id>"
                );
            } else {
                println!("{:<32} ENDPOINT ID", "NAME");
                for (name, id) in file.contacts {
                    println!("{name:<32} {id}");
                }
            }
        }
        ContactCommand::Add { name, peer } => {
            validate_name(&name)?;
            let id = endpoint_id(&peer)?;
            ensure!(
                !file.contacts.contains_key(&name),
                "contact '{name}' already exists; remove it before replacing"
            );
            file.contacts.insert(name.clone(), id);
            save_contacts(&path, &file)?;
            println!("Saved contact '{name}'.");
        }
        ContactCommand::Remove { name } => {
            if file.contacts.remove(&name).is_none() {
                bail!("contact '{name}' was not found");
            }
            save_contacts(&path, &file)?;
            println!("Removed contact '{name}'.");
        }
    }
    Ok(())
}
