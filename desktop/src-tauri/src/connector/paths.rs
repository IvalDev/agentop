//! Path jail and local file operations for the workspace connector.
//!
//! Every relative path from the server is joined to the granted root and
//! canonicalized; anything that resolves outside the root (including symlink
//! escapes) is rejected. The operations only read: `list_files` and
//! `read_file`.

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

    let canonical =
        fs::canonicalize(root.join(candidate)).map_err(|error| OpError::from_io(&error))?;
    if !canonical.starts_with(root) {
        return Err(OpError::invalid_path(
            "La ruta está fuera de la carpeta concedida.",
        ));
    }
    Ok(canonical)
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

/// Executes one request operation against the granted root.
pub fn execute(root: &Path, op: &str, args: &Value) -> Result<Value, OpError> {
    let relative = match args.get("path") {
        None | Some(Value::Null) => "",
        Some(Value::String(value)) => value.as_str(),
        Some(_) => {
            return Err(OpError::invalid_path("El parámetro path debe ser texto."));
        }
    };

    let result = match op {
        "list_files" => serde_json::to_value(list_files(root, relative)?),
        "read_file" => serde_json::to_value(read_file(root, relative)?),
        _ => return Err(OpError::new("io_error", "Operación no soportada.")),
    };

    result.map_err(|error| {
        OpError::new(
            "io_error",
            format!("No se pudo serializar el resultado: {error}"),
        )
    })
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
