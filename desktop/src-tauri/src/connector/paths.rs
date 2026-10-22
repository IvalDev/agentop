//! Path jail and local file operations for the workspace connector.
//!
//! Every relative path from the server is joined to the granted root and
//! canonicalized; anything that resolves outside the root (including symlink
//! escapes) is rejected. Read operations (`list_files`, `read_file`)
//! canonicalize the target itself; write operations (`write_file`,
//! `create_folder`, `move_path`, `delete_path`) canonicalize the destination's
//! parent because the target may not exist yet, and never follow symlinks.

use std::fs;
use std::io::{ErrorKind, Read};
use std::path::{Component, Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use base64::engine::general_purpose::STANDARD as BASE64;
use base64::Engine;
use serde::Serialize;
use serde_json::Value;

/// Maximum entries returned by `list_files`.
pub const MAX_ENTRIES: usize = 1000;

/// Maximum bytes returned by `read_file` (5 MB).
pub const MAX_READ_BYTES: u64 = 5 * 1024 * 1024;

/// Maximum length of a relative path accepted from the server.
pub const MAX_RELATIVE_PATH_LEN: usize = 1024;

/// Error returned to the server inside a `response` message.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct OpError {
    pub code: &'static str,
    pub message: String,
}

impl OpError {
    pub fn new(code: &'static str, message: impl Into<String>) -> Self {
        Self {
            code,
            message: message.into(),
        }
    }

    pub fn invalid_path(message: impl Into<String>) -> Self {
        Self::new("invalid_path", message)
    }

    pub fn already_exists() -> Self {
        Self::new("already_exists", "Ya existe un elemento con ese nombre.")
    }

    /// Maps an I/O failure to one of the contract error codes.
    pub fn from_io(error: &std::io::Error) -> Self {
        match error.kind() {
            ErrorKind::NotFound => {
                Self::new("not_found", "No se encontró el archivo o la carpeta.")
            }
            ErrorKind::PermissionDenied => Self::new(
                "permission_denied",
                "No hay permiso para acceder a esa ruta.",
            ),
            _ => Self::new("io_error", "No se pudo completar la operación."),
        }
    }
}

/// One entry of a directory listing.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct DirEntry {
    pub name: String,
    pub path: String,
    pub kind: &'static str,
    pub size: u64,
    pub modified_at: Option<String>,
}

/// Result of `list_files`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ListResult {
    pub path: String,
    pub entries: Vec<DirEntry>,
}

/// Result of `read_file`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ReadResult {
    pub data_b64: String,
    pub size: u64,
}

/// Result of `write_file`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct WriteResult {
    pub path: String,
    pub bytes: u64,
    pub created: bool,
}

/// Result of `create_folder`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct FolderResult {
    pub path: String,
}

/// Result of `move_path`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct MoveResult {
    pub from_path: String,
    pub to_path: String,
}

/// Result of `delete_path`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct DeleteResult {
    pub path: String,
    pub removed: u64,
}

/// Canonicalizes the folder the user granted and requires it to be a folder.
pub fn canonicalize_root(path: &Path) -> Result<PathBuf, OpError> {
    let canonical = fs::canonicalize(path).map_err(|error| OpError::from_io(&error))?;
    if !canonical.is_dir() {
        return Err(OpError::invalid_path(
            "La ruta concedida no es una carpeta.",
        ));
    }
    Ok(canonical)
}

/// Resolves `relative` inside `root`, rejecting any escape attempt.
///
/// `root` must already be canonical (see [`canonicalize_root`]).
pub fn resolve(root: &Path, relative: &str) -> Result<PathBuf, OpError> {
    validate_relative(relative)?;

    let candidate = Path::new(relative);
    let canonical =
        fs::canonicalize(root.join(candidate)).map_err(|error| OpError::from_io(&error))?;
    if !canonical.starts_with(root) {
        return Err(OpError::invalid_path(
            "La ruta está fuera de la carpeta concedida.",
        ));
    }
    Ok(canonical)
}

/// Resolves a write destination that may not exist yet.
///
/// The leaf's parent directory is canonicalized (it must exist and stay
/// inside the root, so a symlinked parent that escapes is rejected) and the
/// leaf must be a plain file or folder name. The returned path is the final
/// target: the canonical parent joined with the validated leaf.
pub fn resolve_destination(root: &Path, relative: &str) -> Result<PathBuf, OpError> {
    validate_relative(relative)?;

    let candidate = Path::new(relative);
    let leaf = candidate.file_name().ok_or_else(|| {
        OpError::invalid_path("La ruta debe apuntar a un elemento dentro de la carpeta.")
    })?;

    let parent = match candidate.parent() {
        Some(parent) if !parent.as_os_str().is_empty() => {
            let canonical =
                fs::canonicalize(root.join(parent)).map_err(|error| OpError::from_io(&error))?;
            if !canonical.starts_with(root) {
                return Err(OpError::invalid_path(
                    "La ruta está fuera de la carpeta concedida.",
                ));
            }
            canonical
        }
        _ => root.to_path_buf(),
    };

    Ok(parent.join(leaf))
}

/// Validates the shape of a relative path before it touches the filesystem.
fn validate_relative(relative: &str) -> Result<(), OpError> {
    if relative.chars().count() > MAX_RELATIVE_PATH_LEN {
        return Err(OpError::invalid_path("La ruta es demasiado larga."));
    }
    if relative.contains('\0') {
        return Err(OpError::invalid_path(
            "La ruta contiene caracteres inválidos.",
        ));
    }

    let candidate = Path::new(relative);
    if candidate.is_absolute() {
        return Err(OpError::invalid_path("La ruta debe ser relativa."));
    }
    for component in candidate.components() {
        match component {
            Component::ParentDir => {
                return Err(OpError::invalid_path(
                    "La ruta no puede salir de la carpeta.",
                ));
            }
            Component::RootDir | Component::Prefix(_) => {
                return Err(OpError::invalid_path("La ruta debe ser relativa."));
            }
            Component::CurDir | Component::Normal(_) => {}
        }
    }
    Ok(())
}

/// Lists a directory inside the granted root.
pub fn list_files(root: &Path, relative: &str) -> Result<ListResult, OpError> {
    let target = resolve(root, relative)?;
    let directory = fs::read_dir(&target).map_err(|error| OpError::from_io(&error))?;

    let mut entries = Vec::new();
    for item in directory {
        let item = item.map_err(|error| OpError::from_io(&error))?;
        let file_type = item.file_type().map_err(|error| OpError::from_io(&error))?;
        if file_type.is_symlink() || !(file_type.is_file() || file_type.is_dir()) {
            continue;
        }

        let metadata = item.metadata().map_err(|error| OpError::from_io(&error))?;
        let name = item.file_name().to_string_lossy().into_owned();
        let kind = if file_type.is_dir() {
            "directory"
        } else {
            "file"
        };
        let size = if file_type.is_file() {
            metadata.len()
        } else {
            0
        };

        entries.push(DirEntry {
            path: join_relative(relative, &name),
            name,
            kind,
            size,
            modified_at: metadata.modified().ok().and_then(iso8601_utc),
        });
    }

    entries.sort_by(|left, right| left.name.cmp(&right.name));
    entries.truncate(MAX_ENTRIES);

    Ok(ListResult {
        path: relative.to_owned(),
        entries,
    })
}

/// Reads a file inside the granted root as base64.
pub fn read_file(root: &Path, relative: &str) -> Result<ReadResult, OpError> {
    let target = resolve(root, relative)?;
    let metadata = fs::metadata(&target).map_err(|error| OpError::from_io(&error))?;
    if metadata.is_dir() {
        return Err(OpError::new(
            "io_error",
            "La ruta es una carpeta, no un archivo.",
        ));
    }
    if metadata.len() > MAX_READ_BYTES {
        return Err(OpError::new(
            "too_large",
            "El archivo supera el límite de 5 MB.",
        ));
    }

    let file = fs::File::open(&target).map_err(|error| OpError::from_io(&error))?;
    let mut bytes = Vec::new();
    file.take(MAX_READ_BYTES + 1)
        .read_to_end(&mut bytes)
        .map_err(|error| OpError::from_io(&error))?;
    if bytes.len() as u64 > MAX_READ_BYTES {
        return Err(OpError::new(
            "too_large",
            "El archivo supera el límite de 5 MB.",
        ));
    }

    Ok(ReadResult {
        data_b64: BASE64.encode(&bytes),
        size: bytes.len() as u64,
    })
}

/// Creates or overwrites a UTF-8 text file inside the granted root.
///
/// The parent folder must already exist. A symlink at the target is rejected
/// instead of followed, and the byte cap is re-checked on the machine as
/// defence in depth.
pub fn write_file(root: &Path, relative: &str, content: &str) -> Result<WriteResult, OpError> {
    if content.len() > MAX_READ_BYTES as usize {
        return Err(OpError::new(
            "too_large",
            "El archivo supera el límite de 5 MB.",
        ));
    }

    let target = resolve_destination(root, relative)?;
    let existing = fs::symlink_metadata(&target).ok();
    if let Some(metadata) = &existing {
        if metadata.file_type().is_symlink() {
            return Err(OpError::invalid_path(
                "La ruta es un enlace simbólico y no se puede modificar.",
            ));
        }
    }

    fs::write(&target, content).map_err(|error| OpError::from_io(&error))?;

    Ok(WriteResult {
        path: relative.to_owned(),
        bytes: content.len() as u64,
        created: existing.is_none(),
    })
}

/// Creates a folder inside the granted root; fails when something already
/// exists at that path (checked without following symlinks).
pub fn create_folder(root: &Path, relative: &str) -> Result<FolderResult, OpError> {
    let target = resolve_destination(root, relative)?;
    if fs::symlink_metadata(&target).is_ok() {
        return Err(OpError::already_exists());
    }

    fs::create_dir(&target).map_err(|error| OpError::from_io(&error))?;

    Ok(FolderResult {
        path: relative.to_owned(),
    })
}

/// Moves or renames a file, folder or symlink inside the granted root.
///
/// The destination is never overwritten: an existing entry (including a
/// symlink) fails with `already_exists`, and the destination's parent must
/// exist. Symlinks are moved as links and never followed.
pub fn move_path(root: &Path, from: &str, to: &str) -> Result<MoveResult, OpError> {
    let source = resolve_destination(root, from)?;
    let destination = resolve_destination(root, to)?;
    if fs::symlink_metadata(&destination).is_ok() {
        return Err(OpError::already_exists());
    }

    fs::rename(&source, &destination).map_err(|error| OpError::from_io(&error))?;

    Ok(MoveResult {
        from_path: from.to_owned(),
        to_path: to.to_owned(),
    })
}

/// Deletes a file, folder or symlink inside the granted root.
///
/// Folders are removed with all their content (recursive, control total);
/// symlinks are removed as links and never followed. `removed` counts every
/// filesystem entry taken out, the target included.
pub fn delete_path(root: &Path, relative: &str) -> Result<DeleteResult, OpError> {
    let target = resolve_destination(root, relative)?;
    let metadata = fs::symlink_metadata(&target).map_err(|error| OpError::from_io(&error))?;

    let removed = if metadata.file_type().is_dir() {
        remove_tree(&target)? + 1
    } else {
        fs::remove_file(&target).map_err(|error| OpError::from_io(&error))?;
        1
    };

    Ok(DeleteResult {
        path: relative.to_owned(),
        removed,
    })
}

/// Removes a directory and everything inside without following symlinks.
///
/// Returns the entries removed under the directory (the directory itself is
/// not counted; the caller adds it).
fn remove_tree(directory: &Path) -> Result<u64, OpError> {
    let entries = fs::read_dir(directory).map_err(|error| OpError::from_io(&error))?;
    let mut removed = 0u64;

    for entry in entries {
        let entry = entry.map_err(|error| OpError::from_io(&error))?;
        // `file_type` never follows symlinks, so a link is removed as a link.
        let file_type = entry
            .file_type()
            .map_err(|error| OpError::from_io(&error))?;
        if file_type.is_dir() {
            removed += remove_tree(&entry.path())?;
        } else {
            fs::remove_file(entry.path()).map_err(|error| OpError::from_io(&error))?;
        }
        removed += 1;
    }

    fs::remove_dir(directory).map_err(|error| OpError::from_io(&error))?;
    Ok(removed)
}

/// Executes one request operation against the granted root.
pub fn execute(root: &Path, op: &str, args: &Value) -> Result<Value, OpError> {
    let result = match op {
        "list_files" => serde_json::to_value(list_files(root, optional_path(args)?)?),
        "read_file" => serde_json::to_value(read_file(root, optional_path(args)?)?),
        "write_file" => serde_json::to_value(write_file(
            root,
            required_arg(args, "path")?,
            required_arg(args, "content")?,
        )?),
        "create_folder" => serde_json::to_value(create_folder(root, required_arg(args, "path")?)?),
        "move_path" => serde_json::to_value(move_path(
            root,
            required_arg(args, "from_path")?,
            required_arg(args, "to_path")?,
        )?),
        "delete_path" => serde_json::to_value(delete_path(root, required_arg(args, "path")?)?),
        _ => return Err(OpError::new("io_error", "Operación no soportada.")),
    };

    result.map_err(|error| {
        OpError::new(
            "io_error",
            format!("No se pudo serializar el resultado: {error}"),
        )
    })
}

/// Reads the optional `path` argument; `list_files` and `read_file` default
/// it to the workspace root.
fn optional_path(args: &Value) -> Result<&str, OpError> {
    match args.get("path") {
        None | Some(Value::Null) => Ok(""),
        Some(Value::String(value)) => Ok(value.as_str()),
        Some(_) => Err(OpError::invalid_path("El parámetro path debe ser texto.")),
    }
}

/// Reads a required string argument such as `content`, `from_path` or
/// `to_path`.
fn required_arg<'a>(args: &'a Value, name: &str) -> Result<&'a str, OpError> {
    match args.get(name) {
        Some(Value::String(value)) => Ok(value.as_str()),
        _ => Err(OpError::invalid_path(format!(
            "El parámetro {name} debe ser texto."
        ))),
    }
}

/// Formats a filesystem timestamp as UTC ISO-8601 (`2023-11-14T22:13:20Z`).
fn iso8601_utc(time: SystemTime) -> Option<String> {
    let seconds = time.duration_since(UNIX_EPOCH).ok()?.as_secs();
    let days = (seconds / 86_400) as i64;
    let remaining = seconds % 86_400;
    let (year, month, day) = civil_from_days(days);
    let hour = remaining / 3_600;
    let minute = (remaining % 3_600) / 60;
    let second = remaining % 60;

    Some(format!(
        "{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}Z"
    ))
}

/// Converts days since the Unix epoch to a civil date (Howard Hinnant's
/// `civil_from_days`).
fn civil_from_days(days: i64) -> (i64, u32, u32) {
    let shifted = days + 719_468;
    let era = if shifted >= 0 {
        shifted
    } else {
        shifted - 146_096
    } / 146_097;
    let day_of_era = (shifted - era * 146_097) as u64;
    let year_of_era =
        (day_of_era - day_of_era / 1_460 + day_of_era / 36_524 - day_of_era / 146_096) / 365;
    let year = year_of_era as i64 + era * 400;
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let month_position = (5 * day_of_year + 2) / 153;
    let day = (day_of_year - (153 * month_position + 2) / 5 + 1) as u32;
    let month = if month_position < 10 {
        month_position + 3
    } else {
        month_position - 9
    } as u32;

    (if month <= 2 { year + 1 } else { year }, month, day)
}

/// Joins a request path with an entry name using `/` separators.
fn join_relative(base: &str, name: &str) -> String {
    let base = base.trim_matches('/');
    if base.is_empty() || base == "." {
        name.to_owned()
    } else {
        format!("{base}/{name}")
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::connector::test_support::TempDir;
    use serde_json::json;

    struct Fixture {
        _dir: TempDir,
        root: PathBuf,
    }

    fn fixture(name: &str) -> Fixture {
        let dir = TempDir::new(name);
        let root = dir.path().join("root");
        fs::create_dir_all(root.join("sub")).expect("fixture directory must be created");
        fs::write(root.join("b.txt"), b"bravo").expect("fixture must be written");
        fs::write(root.join("a.md"), b"alfa").expect("fixture must be written");
        fs::write(root.join("sub").join("c.txt"), b"charlie").expect("fixture must be written");
        fs::write(dir.path().join("outside.txt"), b"secret").expect("fixture must be written");

        let root = fs::canonicalize(root).expect("fixture root must canonicalize");
        Fixture { _dir: dir, root }
    }

    fn error_code(result: Result<impl std::fmt::Debug, OpError>) -> &'static str {
        result.expect_err("operation must fail").code
    }

    #[test]
    fn canonicalize_root_requires_a_folder_and_canonicalizes_it() {
        let fixture = fixture("canonicalize-root");

        assert_eq!(
            canonicalize_root(&fixture.root).expect("folder must canonicalize"),
            fixture.root
        );
        assert_eq!(
            error_code(canonicalize_root(&fixture.root.join("a.md"))),
            "invalid_path",
            "a file cannot be granted as the workspace"
        );
        assert_eq!(
            error_code(canonicalize_root(&fixture.root.join("missing"))),
            "not_found"
        );
    }

    #[test]
    fn resolve_accepts_paths_inside_the_root() {
        let fixture = fixture("resolve-inside");

        for relative in ["", ".", "a.md", "sub", "sub/c.txt"] {
            let resolved = resolve(&fixture.root, relative).expect("path must resolve");
            assert!(resolved.starts_with(&fixture.root), "relative={relative:?}");
        }
        assert_eq!(
            resolve(&fixture.root, "a.md").expect("path must resolve"),
            fixture.root.join("a.md")
        );
    }

    #[test]
    fn resolve_rejects_parent_components_even_when_they_stay_inside() {
        let fixture = fixture("resolve-parent");

        for relative in [
            "..",
            "../outside.txt",
            "sub/../../outside.txt",
            "sub/../a.md",
        ] {
            assert_eq!(
                error_code(resolve(&fixture.root, relative)),
                "invalid_path",
                "relative={relative:?}"
            );
        }
    }

    #[test]
    fn resolve_rejects_absolute_paths() {
        let fixture = fixture("resolve-absolute");

        for relative in ["/etc/passwd", "/", "//etc/passwd"] {
            assert_eq!(
                error_code(resolve(&fixture.root, relative)),
                "invalid_path",
                "relative={relative:?}"
            );
        }
    }

    #[test]
    fn resolve_rejects_nul_bytes_and_overlong_paths() {
        let fixture = fixture("resolve-invalid");

        assert_eq!(error_code(resolve(&fixture.root, "a\0b")), "invalid_path");
        assert_eq!(
            error_code(resolve(
                &fixture.root,
                &"a".repeat(MAX_RELATIVE_PATH_LEN + 1)
            )),
            "invalid_path"
        );
        assert_ne!(
            error_code(resolve(&fixture.root, &"a".repeat(MAX_RELATIVE_PATH_LEN))),
            "invalid_path",
            "the length boundary itself is accepted"
        );
        assert_ne!(
            error_code(resolve(&fixture.root, &"á".repeat(MAX_RELATIVE_PATH_LEN))),
            "invalid_path",
            "the limit counts characters, like the server contract"
        );
        assert_eq!(
            error_code(resolve(
                &fixture.root,
                &"á".repeat(MAX_RELATIVE_PATH_LEN + 1)
            )),
            "invalid_path"
        );
    }

    #[cfg(unix)]
    #[test]
    fn resolve_rejects_symlinks_that_escape_the_root() {
        use std::os::unix::fs::symlink;

        let fixture = fixture("resolve-symlink");
        let outside_dir = fixture._dir.path().join("outside");
        fs::create_dir_all(&outside_dir).expect("outside directory must be created");
        fs::write(outside_dir.join("secret.txt"), b"secret").expect("fixture must be written");
        symlink(
            fixture._dir.path().join("outside.txt"),
            fixture.root.join("link.txt"),
        )
        .expect("symlink must be created");
        symlink(&outside_dir, fixture.root.join("link-dir")).expect("symlink must be created");

        for relative in ["link.txt", "link-dir", "link-dir/secret.txt"] {
            assert_eq!(
                error_code(resolve(&fixture.root, relative)),
                "invalid_path",
                "relative={relative:?}"
            );
        }
    }

    #[test]
    fn resolve_reports_missing_paths_as_not_found() {
        let fixture = fixture("resolve-missing");

        assert_eq!(
            error_code(resolve(&fixture.root, "missing.txt")),
            "not_found"
        );
    }

    #[test]
    fn list_files_is_sorted_and_skips_symlinks() {
        #[cfg(unix)]
        use std::os::unix::fs::symlink;

        let fixture = fixture("list");
        #[cfg(unix)]
        symlink(
            fixture._dir.path().join("outside.txt"),
            fixture.root.join("z-link"),
        )
        .expect("symlink must be created");

        let listing = list_files(&fixture.root, "").expect("listing must succeed");

        let names: Vec<&str> = listing.entries.iter().map(|e| e.name.as_str()).collect();
        assert_eq!(names, ["a.md", "b.txt", "sub"]);
        assert_eq!(listing.entries[0].kind, "file");
        assert_eq!(listing.entries[0].size, 4);
        assert_eq!(listing.entries[2].kind, "directory");
        assert_eq!(listing.entries[2].size, 0);
        assert_eq!(listing.entries[0].path, "a.md");
    }

    #[test]
    fn list_files_returns_paths_relative_to_the_root() {
        let fixture = fixture("list-nested");

        let listing = list_files(&fixture.root, "sub").expect("listing must succeed");

        assert_eq!(listing.path, "sub");
        assert_eq!(listing.entries.len(), 1);
        assert_eq!(listing.entries[0].name, "c.txt");
        assert_eq!(listing.entries[0].path, "sub/c.txt");
        assert_eq!(listing.entries[0].kind, "file");
        assert_eq!(listing.entries[0].size, 7);
        assert!(listing.entries[0].modified_at.is_some());
    }

    #[test]
    fn list_files_caps_the_result_at_one_thousand_sorted_entries() {
        let fixture = fixture("list-cap");
        let many = fixture.root.join("many");
        fs::create_dir_all(&many).expect("fixture directory must be created");
        for index in 0..(MAX_ENTRIES + 5) {
            fs::write(many.join(format!("f{index:04}")), b"x").expect("fixture must be written");
        }

        let listing = list_files(&fixture.root, "many").expect("listing must succeed");

        assert_eq!(listing.entries.len(), MAX_ENTRIES);
        assert_eq!(listing.entries[0].name, "f0000");
        assert_eq!(
            listing.entries[MAX_ENTRIES - 1].name,
            format!("f{:04}", MAX_ENTRIES - 1)
        );
    }

    #[test]
    fn read_file_returns_the_bytes_in_base64_with_the_size() {
        let fixture = fixture("read");

        let read = read_file(&fixture.root, "b.txt").expect("read must succeed");

        assert_eq!(read.size, 5);
        assert_eq!(read.data_b64, "YnJhdm8=");
        assert_eq!(
            BASE64.decode(&read.data_b64).expect("base64 must decode"),
            b"bravo"
        );
    }

    #[test]
    fn read_file_rejects_files_over_the_limit() {
        let fixture = fixture("read-large");
        let large = fixture.root.join("large.bin");
        let file = fs::File::create(&large).expect("fixture must be created");
        file.set_len(MAX_READ_BYTES + 1).expect("fixture must grow");

        assert_eq!(
            error_code(read_file(&fixture.root, "large.bin")),
            "too_large"
        );
    }

    #[test]
    fn read_file_reports_directories_and_missing_files() {
        let fixture = fixture("read-errors");

        assert_eq!(error_code(read_file(&fixture.root, "sub")), "io_error");
        assert_eq!(
            error_code(read_file(&fixture.root, "missing.txt")),
            "not_found"
        );
    }

    #[test]
    fn io_errors_map_to_the_contract_codes() {
        for (kind, code) in [
            (ErrorKind::NotFound, "not_found"),
            (ErrorKind::PermissionDenied, "permission_denied"),
            (ErrorKind::InvalidData, "io_error"),
            (ErrorKind::Other, "io_error"),
        ] {
            let error = OpError::from_io(&std::io::Error::from(kind));
            assert_eq!(error.code, code, "kind={kind:?}");
        }
    }

    #[test]
    fn execute_dispatches_list_and_read() {
        let fixture = fixture("execute");

        let listed =
            execute(&fixture.root, "list_files", &json!({"path": ""})).expect("list must succeed");
        assert_eq!(listed["path"], "");
        assert_eq!(
            listed["entries"].as_array().expect("entries array").len(),
            3
        );

        let read = execute(&fixture.root, "read_file", &json!({"path": "a.md"}))
            .expect("read must succeed");
        assert_eq!(read["size"], 4);
        assert_eq!(read["data_b64"], BASE64.encode(b"alfa"));
    }

    #[test]
    fn execute_defaults_to_the_root_and_rejects_bad_arguments() {
        let fixture = fixture("execute-args");

        let listed = execute(&fixture.root, "list_files", &json!({})).expect("list must succeed");
        assert_eq!(
            listed["entries"].as_array().expect("entries array").len(),
            3
        );

        assert_eq!(
            execute(&fixture.root, "read_file", &json!({"path": 42})).expect_err("must fail"),
            OpError::invalid_path("El parámetro path debe ser texto.")
        );
        assert_eq!(
            execute(&fixture.root, "delete_file", &json!({"path": "a.md"}))
                .expect_err("must fail")
                .code,
            "io_error"
        );
    }

    #[test]
    fn iso8601_formats_utc_epochs() {
        assert_eq!(
            iso8601_utc(UNIX_EPOCH).as_deref(),
            Some("1970-01-01T00:00:00Z")
        );
        assert_eq!(
            iso8601_utc(UNIX_EPOCH + std::time::Duration::from_secs(1_700_000_000)).as_deref(),
            Some("2023-11-14T22:13:20Z")
        );
    }

    #[test]
    fn join_relative_uses_single_separators() {
        assert_eq!(join_relative("", "a.txt"), "a.txt");
        assert_eq!(join_relative("sub", "a.txt"), "sub/a.txt");
        assert_eq!(join_relative("sub/", "a.txt"), "sub/a.txt");
    }

    #[test]
    fn unknown_operations_stay_unsupported() {
        let fixture = fixture("unknown-op");

        let error = execute(&fixture.root, "export_everything", &json!({}))
            .expect_err("unknown operation must fail");
        assert_eq!(error, OpError::new("io_error", "Operación no soportada."));
    }

    #[test]
    fn write_file_creates_and_overwrites_with_the_contract_shape() {
        let fixture = fixture("write-create");

        let created = execute(
            &fixture.root,
            "write_file",
            &json!({"path": "sub/nuevo.txt", "content": "hola"}),
        )
        .expect("create must succeed");
        assert_eq!(
            created,
            json!({"path": "sub/nuevo.txt", "bytes": 4, "created": true})
        );
        assert_eq!(
            fs::read_to_string(fixture.root.join("sub/nuevo.txt")).expect("file must exist"),
            "hola"
        );

        let overwrote = execute(
            &fixture.root,
            "write_file",
            &json!({"path": "sub/nuevo.txt", "content": "adiós"}),
        )
        .expect("overwrite must succeed");
        assert_eq!(
            overwrote,
            json!({"path": "sub/nuevo.txt", "bytes": 6, "created": false})
        );
        assert_eq!(
            fs::read_to_string(fixture.root.join("sub/nuevo.txt")).expect("file must exist"),
            "adiós"
        );
    }

    #[test]
    fn write_file_requires_an_existing_parent_folder() {
        let fixture = fixture("write-parent");

        let error = execute(
            &fixture.root,
            "write_file",
            &json!({"path": "falta/archivo.txt", "content": "x"}),
        )
        .expect_err("missing parent must fail");
        assert_eq!(error.code, "not_found");
        assert!(!fixture.root.join("falta").exists());
    }

    #[test]
    fn write_file_enforces_the_byte_cap_before_touching_the_disk() {
        let fixture = fixture("write-cap");
        fs::write(fixture.root.join("grande.txt"), b"original").expect("fixture must be written");
        let oversized = "a".repeat(MAX_READ_BYTES as usize + 1);

        let error = execute(
            &fixture.root,
            "write_file",
            &json!({"path": "grande.txt", "content": oversized}),
        )
        .expect_err("oversized content must fail");
        assert_eq!(error.code, "too_large");
        assert_eq!(
            fs::read_to_string(fixture.root.join("grande.txt")).expect("file must exist"),
            "original",
            "the rejected write must leave the file untouched"
        );

        let error = execute(
            &fixture.root,
            "write_file",
            &json!({"path": "nuevo-grande.txt", "content": oversized}),
        )
        .expect_err("oversized content must fail");
        assert_eq!(error.code, "too_large");
        assert!(!fixture.root.join("nuevo-grande.txt").exists());
    }

    #[test]
    fn write_file_requires_text_content() {
        let fixture = fixture("write-args");

        let error = execute(&fixture.root, "write_file", &json!({"path": "ok.txt"}))
            .expect_err("missing content must fail");
        assert_eq!(
            error,
            OpError::invalid_path("El parámetro content debe ser texto.")
        );
        let error = execute(
            &fixture.root,
            "write_file",
            &json!({"path": "ok.txt", "content": 7}),
        )
        .expect_err("non-text content must fail");
        assert_eq!(error.code, "invalid_path");
    }

    #[test]
    fn create_folder_creates_and_reports_existing_elements() {
        let fixture = fixture("create-folder");

        let created = execute(&fixture.root, "create_folder", &json!({"path": "nueva"}))
            .expect("create must succeed");
        assert_eq!(created, json!({"path": "nueva"}));
        assert!(fixture.root.join("nueva").is_dir());

        let again = execute(&fixture.root, "create_folder", &json!({"path": "nueva"}))
            .expect_err("existing folder must fail");
        assert_eq!(again.code, "already_exists");

        let existing = execute(&fixture.root, "create_folder", &json!({"path": "sub"}))
            .expect_err("existing entry must fail");
        assert_eq!(existing.code, "already_exists");

        assert_eq!(
            error_code(execute(
                &fixture.root,
                "create_folder",
                &json!({"path": "falta/nueva"})
            )),
            "not_found"
        );
    }

    #[test]
    fn move_path_renames_moves_and_never_overwrites() {
        let fixture = fixture("move");

        let renamed = execute(
            &fixture.root,
            "move_path",
            &json!({"from_path": "b.txt", "to_path": "bravo.txt"}),
        )
        .expect("rename must succeed");
        assert_eq!(
            renamed,
            json!({"from_path": "b.txt", "to_path": "bravo.txt"})
        );
        assert!(fixture.root.join("bravo.txt").exists());
        assert!(!fixture.root.join("b.txt").exists());

        let moved = execute(
            &fixture.root,
            "move_path",
            &json!({"from_path": "a.md", "to_path": "sub/a.md"}),
        )
        .expect("move must succeed");
        assert_eq!(moved["to_path"], "sub/a.md");
        assert!(fixture.root.join("sub/a.md").exists());

        let folder = execute(
            &fixture.root,
            "move_path",
            &json!({"from_path": "sub", "to_path": "renombrada"}),
        )
        .expect("folder move must succeed");
        assert_eq!(folder["to_path"], "renombrada");
        assert!(fixture.root.join("renombrada/a.md").exists());

        fs::write(fixture.root.join("uno.txt"), b"1").expect("fixture must be written");
        fs::write(fixture.root.join("dos.txt"), b"2").expect("fixture must be written");
        let error = execute(
            &fixture.root,
            "move_path",
            &json!({"from_path": "uno.txt", "to_path": "dos.txt"}),
        )
        .expect_err("existing destination must fail");
        assert_eq!(error.code, "already_exists");
        assert_eq!(
            fs::read_to_string(fixture.root.join("uno.txt")).expect("file must exist"),
            "1"
        );
        assert_eq!(
            fs::read_to_string(fixture.root.join("dos.txt")).expect("file must exist"),
            "2"
        );
    }

    #[test]
    fn move_path_requires_an_existing_source_and_destination_parent() {
        let fixture = fixture("move-missing");

        assert_eq!(
            error_code(execute(
                &fixture.root,
                "move_path",
                &json!({"from_path": "fantasma.txt", "to_path": "ok.txt"})
            )),
            "not_found"
        );
        assert_eq!(
            error_code(execute(
                &fixture.root,
                "move_path",
                &json!({"from_path": "a.md", "to_path": "falta/a.md"})
            )),
            "not_found"
        );
        assert!(fixture.root.join("a.md").exists());
    }

    #[test]
    fn delete_path_removes_files_and_folders_reporting_entries() {
        let fixture = fixture("delete");

        let deleted = execute(&fixture.root, "delete_path", &json!({"path": "b.txt"}))
            .expect("delete must succeed");
        assert_eq!(deleted, json!({"path": "b.txt", "removed": 1}));
        assert!(!fixture.root.join("b.txt").exists());

        // `sub` holds `c.txt`: the folder plus its child are two entries.
        let deleted = execute(&fixture.root, "delete_path", &json!({"path": "sub"}))
            .expect("delete must succeed");
        assert_eq!(deleted, json!({"path": "sub", "removed": 2}));
        assert!(!fixture.root.join("sub").exists());

        assert_eq!(
            error_code(execute(
                &fixture.root,
                "delete_path",
                &json!({"path": "sub"})
            )),
            "not_found"
        );
    }

    #[test]
    fn write_ops_reject_escaping_paths() {
        let fixture = fixture("write-jail");

        for relative in ["../fuera.txt", "/etc/passwd", "a\0b"] {
            assert_eq!(
                error_code(execute(
                    &fixture.root,
                    "write_file",
                    &json!({"path": relative, "content": "x"})
                )),
                "invalid_path",
                "relative={relative:?}"
            );
            assert_eq!(
                error_code(execute(
                    &fixture.root,
                    "create_folder",
                    &json!({"path": relative})
                )),
                "invalid_path",
                "relative={relative:?}"
            );
            assert_eq!(
                error_code(execute(
                    &fixture.root,
                    "delete_path",
                    &json!({"path": relative})
                )),
                "invalid_path",
                "relative={relative:?}"
            );
            assert_eq!(
                error_code(execute(
                    &fixture.root,
                    "move_path",
                    &json!({"from_path": relative, "to_path": "ok.txt"})
                )),
                "invalid_path",
                "from={relative:?}"
            );
            assert_eq!(
                error_code(execute(
                    &fixture.root,
                    "move_path",
                    &json!({"from_path": "a.md", "to_path": relative})
                )),
                "invalid_path",
                "to={relative:?}"
            );
        }

        let overlong = "a".repeat(MAX_RELATIVE_PATH_LEN + 1);
        assert_eq!(
            error_code(execute(
                &fixture.root,
                "write_file",
                &json!({"path": overlong, "content": "x"})
            )),
            "invalid_path"
        );
    }

    #[test]
    fn write_ops_cannot_target_the_workspace_root() {
        let fixture = fixture("write-root");

        for relative in ["", "."] {
            assert_eq!(
                error_code(execute(
                    &fixture.root,
                    "write_file",
                    &json!({"path": relative, "content": "x"})
                )),
                "invalid_path",
                "relative={relative:?}"
            );
            assert_eq!(
                error_code(execute(
                    &fixture.root,
                    "create_folder",
                    &json!({"path": relative})
                )),
                "invalid_path",
                "relative={relative:?}"
            );
            assert_eq!(
                error_code(execute(
                    &fixture.root,
                    "delete_path",
                    &json!({"path": relative})
                )),
                "invalid_path",
                "relative={relative:?}"
            );
            assert_eq!(
                error_code(execute(
                    &fixture.root,
                    "move_path",
                    &json!({"from_path": "a.md", "to_path": relative})
                )),
                "invalid_path",
                "to={relative:?}"
            );
        }
        assert!(
            fixture.root.join("a.md").exists(),
            "the workspace must stay intact"
        );
    }

    #[cfg(unix)]
    #[test]
    fn write_file_never_writes_through_symlinks() {
        use std::os::unix::fs::symlink;

        let fixture = fixture("write-symlink");
        symlink(
            fixture._dir.path().join("outside.txt"),
            fixture.root.join("enlace.txt"),
        )
        .expect("symlink must be created");

        let error = execute(
            &fixture.root,
            "write_file",
            &json!({"path": "enlace.txt", "content": "nuevo"}),
        )
        .expect_err("writing through a symlink must fail");
        assert_eq!(error.code, "invalid_path");
        assert_eq!(
            fs::read_to_string(fixture._dir.path().join("outside.txt")).expect("file must exist"),
            "secret",
            "the outside target must stay untouched"
        );
    }

    #[cfg(unix)]
    #[test]
    fn move_path_moves_a_symlink_as_a_link() {
        use std::os::unix::fs::symlink;

        let fixture = fixture("move-symlink");
        symlink(fixture.root.join("a.md"), fixture.root.join("enlace.md"))
            .expect("symlink must be created");

        let moved = execute(
            &fixture.root,
            "move_path",
            &json!({"from_path": "enlace.md", "to_path": "sub/enlace.md"}),
        )
        .expect("link move must succeed");
        assert_eq!(
            moved,
            json!({"from_path": "enlace.md", "to_path": "sub/enlace.md"})
        );
        assert_eq!(
            fs::read_to_string(fixture.root.join("a.md")).expect("file must exist"),
            "alfa",
            "the link target must stay in place"
        );
        assert!(fs::symlink_metadata(fixture.root.join("sub/enlace.md"))
            .expect("moved link must exist")
            .file_type()
            .is_symlink());

        symlink(fixture.root.join("a.md"), fixture.root.join("ocupado.md"))
            .expect("symlink must be created");
        let error = execute(
            &fixture.root,
            "move_path",
            &json!({"from_path": "b.txt", "to_path": "ocupado.md"}),
        )
        .expect_err("existing symlink destination must fail");
        assert_eq!(error.code, "already_exists");
    }

    #[cfg(unix)]
    #[test]
    fn delete_path_never_follows_symlinks() {
        use std::os::unix::fs::symlink;

        let fixture = fixture("delete-symlink");
        let outside_dir = fixture._dir.path().join("outside-delete");
        fs::create_dir_all(&outside_dir).expect("outside directory must be created");
        fs::write(outside_dir.join("secret.txt"), b"secret").expect("fixture must be written");

        let folder = fixture.root.join("todo");
        fs::create_dir_all(folder.join("interno")).expect("fixture directory must be created");
        fs::write(folder.join("interno").join("x.txt"), b"x").expect("fixture must be written");
        symlink(&outside_dir, folder.join("enlace")).expect("symlink must be created");

        let deleted = execute(&fixture.root, "delete_path", &json!({"path": "todo"}))
            .expect("delete must succeed");
        // todo + interno + x.txt + enlace = 4 entries, outside target untouched.
        assert_eq!(deleted, json!({"path": "todo", "removed": 4}));
        assert!(!folder.exists());
        assert!(
            outside_dir.join("secret.txt").exists(),
            "the symlink target outside the root must survive"
        );
    }
}

#[cfg(unix)]
#[cfg(test)]
mod v1_probe_tests {
    //! Independent L1 probes: path jail and read caps against the granted root.
    use super::*;
    use crate::connector::test_support::TempDir;
    use std::os::unix::fs::symlink;

    #[test]
    fn v1_probe_jail_rejects_every_escape_and_caps_hold() {
        let dir = TempDir::new("v1-probe");
        let raw = dir.path().join("root");
        fs::create_dir_all(&raw).expect("root must be created");
        fs::write(raw.join("ok.md"), b"fine").expect("file must be written");
        let secret = dir.path().join("secret-outside.txt");
        fs::write(&secret, b"leaked").expect("file must be written");
        symlink(&secret, raw.join("escape.txt")).expect("symlink must be created");
        let root = fs::canonicalize(&raw).expect("root must canonicalize");

        assert_eq!(
            resolve(&root, "../secret-outside.txt").unwrap_err().code,
            "invalid_path"
        );
        assert_eq!(
            resolve(&root, "/etc/passwd").unwrap_err().code,
            "invalid_path"
        );
        assert_eq!(
            resolve(&root, "ok.md\0.png").unwrap_err().code,
            "invalid_path"
        );
        assert_eq!(
            resolve(&root, "escape.txt").unwrap_err().code,
            "invalid_path"
        );
        assert!(read_file(&root, "escape.txt").is_err());
        let listing = list_files(&root, "").expect("root listing must work");
        assert!(
            listing
                .entries
                .iter()
                .all(|entry| entry.name != "escape.txt"),
            "symlink leaked into the listing: {listing:?}"
        );
        for entry in &listing.entries {
            assert!(!entry.path.starts_with('/'), "{}", entry.path);
            assert!(
                !entry.path.contains(&root.to_string_lossy()[..]),
                "{}",
                entry.path
            );
        }
        fs::write(raw.join("big.txt"), vec![0u8; MAX_READ_BYTES as usize + 1])
            .expect("big file must be written");
        assert_eq!(read_file(&root, "big.txt").unwrap_err().code, "too_large");
    }
}
