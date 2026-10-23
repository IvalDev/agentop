//! Chunked file transfer for the workspace connector.
//!
//! Large files (mail attachments) cross the WebSocket in pieces of at most
//! [`CHUNK_BYTES`] so no message hits the 16 MiB frame limit. Reads use the
//! same jail as `read_file`; uploads go to a hidden temp file next to the
//! destination and are renamed into place only when complete, so a broken
//! upload never leaves a truncated file behind.

use std::ffi::OsString;
use std::fs;
use std::io::{ErrorKind, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};

use base64::engine::general_purpose::STANDARD as BASE64;
use base64::Engine;
use serde::Serialize;

use super::paths::{resolve, resolve_destination, OpError, ReadResult, WriteResult};

/// Maximum raw bytes moved by one `read_chunk` or `write_chunk` (4 MiB).
pub const CHUNK_BYTES: u64 = 4 * 1024 * 1024;

/// Result of `file_info`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct InfoResult {
    pub path: String,
    pub size: u64,
}

/// Result of `write_chunk`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ChunkResult {
    pub path: String,
    pub upload_id: String,
    pub written: u64,
}

/// Result of `write_abort`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct AbortResult {
    pub path: String,
    pub removed: bool,
}

/// Upload identifier: exactly 32 lowercase hex characters (a `uuid4().hex`).
///
/// It becomes part of the temp file name, so it is validated before any path
/// is built.
struct UploadId<'a>(&'a str);

impl<'a> UploadId<'a> {
    fn parse(value: &'a str) -> Result<Self, OpError> {
        let valid = value.len() == 32
            && value
                .bytes()
                .all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f'));
        if !valid {
            return Err(OpError::invalid_path(
                "El identificador de subida no es válido.",
            ));
        }
        Ok(Self(value))
    }
}

/// Reports the size of a file inside the granted root.
pub fn file_info(root: &Path, relative: &str) -> Result<InfoResult, OpError> {
    let target = resolve(root, relative)?;
    Ok(InfoResult {
        path: relative.to_owned(),
        size: file_size(&target)?,
    })
}

/// Reads `[offset, min(offset + length, size))` of a file as base64, with the
/// file's current total size.
pub fn read_chunk(
    root: &Path,
    relative: &str,
    offset: u64,
    length: u64,
) -> Result<ReadResult, OpError> {
    if length == 0 || length > CHUNK_BYTES {
        return Err(OpError::invalid_path(
            "La longitud del trozo debe estar entre 1 byte y 4 MB.",
        ));
    }

    let target = resolve(root, relative)?;
    let size = file_size(&target)?;
    if offset > size {
        return Err(OpError::invalid_path(
            "La posición supera el tamaño del archivo.",
        ));
    }

    let mut file = fs::File::open(&target).map_err(|error| OpError::from_io(&error))?;
    file.seek(SeekFrom::Start(offset))
        .map_err(|error| OpError::from_io(&error))?;
    let wanted = length.min(size - offset);
    let mut bytes = Vec::with_capacity(wanted as usize);
    file.take(wanted)
        .read_to_end(&mut bytes)
        .map_err(|error| OpError::from_io(&error))?;

    Ok(ReadResult {
        data_b64: BASE64.encode(&bytes),
        size,
    })
}

/// Appends one chunk of an upload to its temp file.
///
/// Offset 0 starts the upload: the destination must not exist (a symlink
/// counts as existing) and the temp file is created exclusively, never
/// truncated. Later chunks must continue exactly where the temp file ends.
pub fn write_chunk(
    root: &Path,
    relative: &str,
    upload_id: &str,
    offset: u64,
    data_b64: &str,
) -> Result<ChunkResult, OpError> {
    let id = UploadId::parse(upload_id)?;
    let data = BASE64
        .decode(data_b64)
        .map_err(|_| OpError::invalid_path("Los datos del trozo no son base64 válido."))?;
    if data.len() as u64 > CHUNK_BYTES {
        return Err(OpError::new(
            "too_large",
            "El trozo supera el límite de 4 MB.",
        ));
    }

    let (destination, temp) = upload_paths(root, relative, &id)?;
    let mut file = if offset == 0 {
        if fs::symlink_metadata(&destination).is_ok() {
            return Err(OpError::already_exists());
        }
        fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&temp)
            .map_err(|error| OpError::from_io(&error))?
    } else {
        if temp_len(&temp) != Some(offset) {
            return Err(upload_gap());
        }
        let file = fs::OpenOptions::new()
            .append(true)
            .open(&temp)
            .map_err(|error| OpError::from_io(&error))?;
        // Re-checked on the open handle in case the temp changed meanwhile.
        let current = file
            .metadata()
            .map_err(|error| OpError::from_io(&error))?
            .len();
        if current != offset {
            return Err(upload_gap());
        }
        file
    };
    file.write_all(&data)
        .map_err(|error| OpError::from_io(&error))?;

    Ok(ChunkResult {
        path: relative.to_owned(),
        upload_id: upload_id.to_owned(),
        written: offset + data.len() as u64,
    })
}

/// Moves a complete upload into place; never overwrites the destination.
pub fn write_commit(
    root: &Path,
    relative: &str,
    upload_id: &str,
    size: u64,
) -> Result<WriteResult, OpError> {
    let id = UploadId::parse(upload_id)?;
    let (destination, temp) = upload_paths(root, relative, &id)?;
    if temp_len(&temp) != Some(size) {
        return Err(OpError::new(
            "io_error",
            "La subida no existe o no está completa.",
        ));
    }
    if fs::symlink_metadata(&destination).is_ok() {
        return Err(OpError::already_exists());
    }

    fs::rename(&temp, &destination).map_err(|error| OpError::from_io(&error))?;

    Ok(WriteResult {
        path: relative.to_owned(),
        bytes: size,
        created: true,
    })
}

/// Removes an upload's temp file if present; calling it again is harmless.
pub fn write_abort(root: &Path, relative: &str, upload_id: &str) -> Result<AbortResult, OpError> {
    let id = UploadId::parse(upload_id)?;
    let (_, temp) = upload_paths(root, relative, &id)?;
    let removed = match fs::remove_file(&temp) {
        Ok(()) => true,
        Err(error) if error.kind() == ErrorKind::NotFound => false,
        Err(error) => return Err(OpError::from_io(&error)),
    };

    Ok(AbortResult {
        path: relative.to_owned(),
        removed,
    })
}

/// Size of a jailed file; folders are rejected like in `read_file`.
fn file_size(target: &Path) -> Result<u64, OpError> {
    let metadata = fs::metadata(target).map_err(|error| OpError::from_io(&error))?;
    if metadata.is_dir() {
        return Err(OpError::new(
            "io_error",
            "La ruta es una carpeta, no un archivo.",
        ));
    }
    Ok(metadata.len())
}

/// Resolves an upload's destination and its temp file
/// `.<file name>.<upload_id>.part` in the same (jailed) folder.
fn upload_paths(
    root: &Path,
    relative: &str,
    id: &UploadId<'_>,
) -> Result<(PathBuf, PathBuf), OpError> {
    let destination = resolve_destination(root, relative)?;
    let (Some(parent), Some(leaf)) = (destination.parent(), destination.file_name()) else {
        return Err(OpError::invalid_path(
            "La ruta debe apuntar a un elemento dentro de la carpeta.",
        ));
    };

    let mut name = OsString::from(".");
    name.push(leaf);
    name.push(".");
    name.push(id.0);
    name.push(".part");
    let temp = parent.join(name);
    Ok((destination, temp))
}

/// Length of the temp file, or `None` when it is missing or not a regular
/// file (a symlink is never followed).
fn temp_len(temp: &Path) -> Option<u64> {
    fs::symlink_metadata(temp)
        .ok()
        .filter(|metadata| metadata.file_type().is_file())
        .map(|metadata| metadata.len())
}

fn upload_gap() -> OpError {
    OpError::new(
        "io_error",
        "La subida no existe o no continúa en esa posición.",
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::connector::paths::{execute, MAX_READ_BYTES};
    use crate::connector::test_support::TempDir;
    use serde_json::{json, Value};

    const ID: &str = "0123456789abcdef0123456789abcdef";

    struct Fixture {
        dir: TempDir,
        root: PathBuf,
    }

    fn fixture(name: &str) -> Fixture {
        let dir = TempDir::new(name);
        let root = dir.path().join("root");
        fs::create_dir_all(root.join("sub")).expect("fixture directory must be created");
        fs::write(root.join("b.txt"), b"bravo").expect("fixture must be written");
        fs::write(dir.path().join("outside.txt"), b"secret").expect("fixture must be written");

        let root = fs::canonicalize(root).expect("fixture root must canonicalize");
        Fixture { dir, root }
    }

    /// Bytes covering every value (invalid UTF-8 included).
    fn binary(len: usize) -> Vec<u8> {
        (0..len).map(|index| (index * 7 % 256) as u8).collect()
    }

    /// Path of the temp file an upload keeps next to its destination.
    fn part(root: &Path, folder: &str, name: &str) -> PathBuf {
        root.join(folder).join(format!(".{name}.{ID}.part"))
    }

    /// Sorted names inside a folder, to prove a rejected op created nothing.
    fn names(folder: &Path) -> Vec<String> {
        let mut names: Vec<String> = fs::read_dir(folder)
            .expect("folder must be listable")
            .map(|entry| {
                entry
                    .expect("entry")
                    .file_name()
                    .to_string_lossy()
                    .into_owned()
            })
            .collect();
        names.sort();
        names
    }

    fn code(result: Result<Value, OpError>) -> &'static str {
        result.expect_err("operation must fail").code
    }

    fn chunk(root: &Path, path: &str, offset: u64, data: &[u8]) -> Result<Value, OpError> {
        execute(
            root,
            "write_chunk",
            &json!({"path": path, "upload_id": ID, "offset": offset, "data_b64": BASE64.encode(data)}),
        )
    }

    fn commit(root: &Path, path: &str, size: u64) -> Result<Value, OpError> {
        execute(
            root,
            "write_commit",
            &json!({"path": path, "upload_id": ID, "size": size}),
        )
    }

    fn abort(root: &Path, path: &str) -> Result<Value, OpError> {
        execute(root, "write_abort", &json!({"path": path, "upload_id": ID}))
    }

    fn read_chunk(root: &Path, path: &str, offset: u64, length: u64) -> Result<Value, OpError> {
        execute(
            root,
            "read_chunk",
            &json!({"path": path, "offset": offset, "length": length}),
        )
    }

    fn decoded(result: &Value) -> Vec<u8> {
        BASE64
            .decode(result["data_b64"].as_str().expect("data_b64 must be text"))
            .expect("data_b64 must decode")
    }

    #[test]
    fn file_info_reports_the_size_of_a_file() {
        let fixture = fixture("info");
        fs::write(fixture.root.join("sub/datos.bin"), binary(1234)).expect("fixture");

        assert_eq!(
            execute(
                &fixture.root,
                "file_info",
                &json!({"path": "sub/datos.bin"})
            )
            .expect("info must succeed"),
            json!({"path": "sub/datos.bin", "size": 1234})
        );
    }

    #[test]
    fn file_info_reports_folders_missing_files_and_escapes() {
        let fixture = fixture("info-errors");

        let folder = execute(&fixture.root, "file_info", &json!({"path": "sub"}))
            .expect_err("a folder must fail");
        assert_eq!(
            folder,
            OpError::new("io_error", "La ruta es una carpeta, no un archivo.")
        );
        assert_eq!(
            code(execute(
                &fixture.root,
                "file_info",
                &json!({"path": "falta.bin"})
            )),
            "not_found"
        );
        for path in ["../outside.txt", "sub/../../outside.txt", "/etc/passwd"] {
            assert_eq!(
                code(execute(&fixture.root, "file_info", &json!({"path": path}))),
                "invalid_path",
                "path={path:?}"
            );
        }
        assert_eq!(
            code(execute(&fixture.root, "file_info", &json!({"path": 7}))),
            "invalid_path"
        );
    }

    #[test]
    fn read_chunk_returns_the_range_and_the_total_size() {
        let fixture = fixture("read-range");
        let content = binary(10);
        fs::write(fixture.root.join("datos.bin"), &content).expect("fixture");

        let first = read_chunk(&fixture.root, "datos.bin", 0, 4).expect("first chunk");
        assert_eq!(first["size"], 10);
        assert_eq!(decoded(&first), &content[0..4]);

        let middle = read_chunk(&fixture.root, "datos.bin", 4, 4).expect("middle chunk");
        assert_eq!(middle["size"], 10);
        assert_eq!(decoded(&middle), &content[4..8]);

        let last = read_chunk(&fixture.root, "datos.bin", 8, 4).expect("last partial chunk");
        assert_eq!(last["size"], 10);
        assert_eq!(decoded(&last), &content[8..10]);

        let whole =
            read_chunk(&fixture.root, "datos.bin", 0, CHUNK_BYTES).expect("full-length chunk");
        assert_eq!(decoded(&whole), content);
    }

    #[test]
    fn read_chunk_at_the_end_returns_empty_data() {
        let fixture = fixture("read-end");
        fs::write(fixture.root.join("datos.bin"), binary(10)).expect("fixture");
        fs::write(fixture.root.join("vacio.bin"), b"").expect("fixture");

        assert_eq!(
            read_chunk(&fixture.root, "datos.bin", 10, 4).expect("end of file"),
            json!({"data_b64": "", "size": 10})
        );
        assert_eq!(
            read_chunk(&fixture.root, "vacio.bin", 0, 4).expect("empty file"),
            json!({"data_b64": "", "size": 0})
        );
    }

    #[test]
    fn read_chunk_rejects_lengths_outside_one_byte_to_chunk_bytes() {
        let fixture = fixture("read-length");
        fs::write(fixture.root.join("datos.bin"), binary(10)).expect("fixture");

        for length in [0, CHUNK_BYTES + 1] {
            assert_eq!(
                code(read_chunk(&fixture.root, "datos.bin", 0, length)),
                "invalid_path",
                "length={length}"
            );
        }
    }

    #[test]
    fn read_chunk_rejects_offsets_past_the_end() {
        let fixture = fixture("read-offset");
        fs::write(fixture.root.join("datos.bin"), binary(10)).expect("fixture");

        assert_eq!(
            code(read_chunk(&fixture.root, "datos.bin", 11, 4)),
            "invalid_path"
        );
        for (offset, length) in [
            (json!(-1), json!(4)),
            (json!("0"), json!(4)),
            (json!(0), json!(1.5)),
        ] {
            assert_eq!(
                code(execute(
                    &fixture.root,
                    "read_chunk",
                    &json!({"path": "datos.bin", "offset": offset, "length": length})
                )),
                "invalid_path",
                "offset={offset} length={length}"
            );
        }
    }

    #[test]
    fn read_chunk_reports_folders_missing_files_and_escapes() {
        let fixture = fixture("read-errors");

        assert_eq!(code(read_chunk(&fixture.root, "sub", 0, 4)), "io_error");
        assert_eq!(
            code(read_chunk(&fixture.root, "falta.bin", 0, 4)),
            "not_found"
        );
        for path in ["../outside.txt", "/etc/passwd"] {
            assert_eq!(
                code(read_chunk(&fixture.root, path, 0, 4)),
                "invalid_path",
                "path={path:?}"
            );
        }
    }

    #[test]
    fn chunked_upload_round_trips_binary_content_byte_for_byte() {
        let fixture = fixture("round-trip");
        let content = binary(2 * CHUNK_BYTES as usize + 1000);
        assert!(
            std::str::from_utf8(&content).is_err(),
            "content must not be text"
        );
        let destination = fixture.root.join("sub/adjunto.bin");
        let temp = part(&fixture.root, "sub", "adjunto.bin");

        let mut offset = 0u64;
        for piece in content.chunks(CHUNK_BYTES as usize) {
            let written = chunk(&fixture.root, "sub/adjunto.bin", offset, piece)
                .expect("chunk must be accepted");
            offset += piece.len() as u64;
            assert_eq!(
                written,
                json!({"path": "sub/adjunto.bin", "upload_id": ID, "written": offset})
            );
            assert_eq!(fs::metadata(&temp).expect("temp file").len(), offset);
            assert!(
                !destination.exists(),
                "nothing is visible before the commit"
            );
        }

        let committed =
            commit(&fixture.root, "sub/adjunto.bin", offset).expect("commit must succeed");
        assert_eq!(
            committed,
            json!({"path": "sub/adjunto.bin", "bytes": offset, "created": true})
        );
        assert_eq!(fs::read(&destination).expect("destination"), content);
        assert!(!temp.exists(), "the temp file is renamed away");
        assert_eq!(names(&fixture.root.join("sub")), ["adjunto.bin"]);

        // The same bytes come back through read_chunk, past read_file's cap.
        let mut read_back = Vec::new();
        while (read_back.len() as u64) < offset {
            let piece = read_chunk(
                &fixture.root,
                "sub/adjunto.bin",
                read_back.len() as u64,
                CHUNK_BYTES,
            )
            .expect("read chunk");
            assert_eq!(piece["size"], offset);
            read_back.extend(decoded(&piece));
        }
        assert_eq!(read_back, content);
    }

    #[test]
    fn an_empty_upload_creates_a_zero_byte_file() {
        let fixture = fixture("empty-upload");

        assert_eq!(
            chunk(&fixture.root, "vacio.bin", 0, b"").expect("empty chunk"),
            json!({"path": "vacio.bin", "upload_id": ID, "written": 0})
        );
        assert_eq!(
            commit(&fixture.root, "vacio.bin", 0).expect("commit"),
            json!({"path": "vacio.bin", "bytes": 0, "created": true})
        );
        assert_eq!(fs::read(fixture.root.join("vacio.bin")).expect("file"), b"");
        assert!(!part(&fixture.root, "", "vacio.bin").exists());
    }

    #[test]
    fn write_chunk_rejects_offsets_that_do_not_continue_the_temp_file() {
        let fixture = fixture("out-of-order");
        let temp = part(&fixture.root, "", "datos.bin");

        assert_eq!(
            code(chunk(&fixture.root, "datos.bin", 3, b"abc")),
            "io_error",
            "a continuation without a started upload must fail"
        );
        assert!(!temp.exists());

        chunk(&fixture.root, "datos.bin", 0, b"abc").expect("first chunk");
        for offset in [2, 4, 9] {
            assert_eq!(
                code(chunk(&fixture.root, "datos.bin", offset, b"xyz")),
                "io_error",
                "offset={offset}"
            );
        }
        assert_eq!(
            fs::read(&temp).expect("temp"),
            b"abc",
            "the temp is untouched"
        );

        assert_eq!(
            code(chunk(&fixture.root, "datos.bin", 0, b"zz")),
            "io_error",
            "restarting an upload id must never truncate its temp file"
        );
        assert_eq!(fs::read(&temp).expect("temp"), b"abc");

        assert_eq!(
            chunk(&fixture.root, "datos.bin", 3, b"").expect("empty continuation"),
            json!({"path": "datos.bin", "upload_id": ID, "written": 3})
        );
        chunk(&fixture.root, "datos.bin", 3, b"de").expect("next chunk");
        assert_eq!(fs::read(&temp).expect("temp"), b"abcde");
        assert!(!fixture.root.join("datos.bin").exists());
    }

    #[test]
    fn write_chunk_refuses_an_existing_destination() {
        let fixture = fixture("existing");

        for path in ["b.txt", "sub"] {
            let error = chunk(&fixture.root, path, 0, b"nuevo").expect_err("must fail");
            assert_eq!(error, OpError::already_exists(), "path={path:?}");
        }
        assert_eq!(
            fs::read(fixture.root.join("b.txt")).expect("file"),
            b"bravo"
        );
        assert_eq!(
            names(&fixture.root),
            ["b.txt", "sub"],
            "no temp file is created"
        );
    }

    #[cfg(unix)]
    #[test]
    fn write_chunk_and_commit_refuse_symlink_destinations() {
        use std::os::unix::fs::symlink;

        let fixture = fixture("symlink-destination");
        let outside = fixture.dir.path().join("outside.txt");
        symlink(&outside, fixture.root.join("enlace.bin")).expect("symlink");
        symlink(
            fixture.dir.path().join("nada"),
            fixture.root.join("roto.bin"),
        )
        .expect("symlink");

        for path in ["enlace.bin", "roto.bin"] {
            assert_eq!(
                code(chunk(&fixture.root, path, 0, b"nuevo")),
                "already_exists",
                "path={path:?}"
            );
        }
        assert_eq!(
            names(&fixture.root),
            ["b.txt", "enlace.bin", "roto.bin", "sub"]
        );

        chunk(&fixture.root, "luego.bin", 0, b"nuevo").expect("chunk");
        symlink(&outside, fixture.root.join("luego.bin")).expect("symlink");
        assert_eq!(
            code(commit(&fixture.root, "luego.bin", 5)),
            "already_exists"
        );
        assert_eq!(fs::read(&outside).expect("outside"), b"secret");
        assert!(fs::symlink_metadata(fixture.root.join("luego.bin"))
            .expect("link")
            .file_type()
            .is_symlink());
        assert!(!fixture.dir.path().join("nada").exists());
    }

    #[test]
    fn upload_ops_reject_destinations_outside_the_jail() {
        let fixture = fixture("upload-jail");

        for path in [
            "../fuera.bin",
            "sub/../../fuera.bin",
            "/tmp/fuera.bin",
            "",
            ".",
        ] {
            assert_eq!(
                code(chunk(&fixture.root, path, 0, b"x")),
                "invalid_path",
                "{path:?}"
            );
            assert_eq!(
                code(commit(&fixture.root, path, 1)),
                "invalid_path",
                "{path:?}"
            );
            assert_eq!(code(abort(&fixture.root, path)), "invalid_path", "{path:?}");
        }
        assert_eq!(
            code(chunk(&fixture.root, "falta/x.bin", 0, b"x")),
            "not_found",
            "the destination folder must exist"
        );
        assert_eq!(names(&fixture.root), ["b.txt", "sub"]);
        assert_eq!(names(fixture.dir.path()), ["outside.txt", "root"]);
    }

    #[cfg(unix)]
    #[test]
    fn upload_ops_reject_a_parent_symlinked_outside_the_jail() {
        use std::os::unix::fs::symlink;

        let fixture = fixture("upload-parent-link");
        let outside = fixture.dir.path().join("fuera");
        fs::create_dir(&outside).expect("outside folder");
        symlink(&outside, fixture.root.join("escape")).expect("symlink");

        assert_eq!(
            code(chunk(&fixture.root, "escape/x.bin", 0, b"x")),
            "invalid_path"
        );
        assert_eq!(
            code(commit(&fixture.root, "escape/x.bin", 1)),
            "invalid_path"
        );
        assert_eq!(code(abort(&fixture.root, "escape/x.bin")), "invalid_path");
        assert!(
            names(&outside).is_empty(),
            "nothing is written outside the root"
        );
    }

    #[test]
    fn upload_ops_reject_malformed_upload_ids() {
        let fixture = fixture("upload-id");

        for upload_id in [
            json!("0123456789ABCDEF0123456789ABCDEF"),
            json!("0123456789abcdef0123456789abcde"),
            json!("0123456789abcdef0123456789abcdef0"),
            json!("0123456789abcdef0123456789abcdeg"),
            json!("../../../../../../../tmp/evil0000"),
            json!(""),
            json!(42),
        ] {
            let chunk_args =
                json!({"path": "x.bin", "upload_id": upload_id, "offset": 0, "data_b64": "eA=="});
            let commit_args = json!({"path": "x.bin", "upload_id": upload_id, "size": 1});
            let abort_args = json!({"path": "x.bin", "upload_id": upload_id});
            assert_eq!(
                code(execute(&fixture.root, "write_chunk", &chunk_args)),
                "invalid_path",
                "{upload_id}"
            );
            assert_eq!(
                code(execute(&fixture.root, "write_commit", &commit_args)),
                "invalid_path",
                "{upload_id}"
            );
            assert_eq!(
                code(execute(&fixture.root, "write_abort", &abort_args)),
                "invalid_path",
                "{upload_id}"
            );
        }
        assert_eq!(names(&fixture.root), ["b.txt", "sub"]);
    }

    #[test]
    fn write_chunk_rejects_invalid_base64() {
        let fixture = fixture("bad-base64");

        for data in ["no es base64!", "eA", "eA=", "eA==\n"] {
            let args = json!({"path": "x.bin", "upload_id": ID, "offset": 0, "data_b64": data});
            assert_eq!(
                code(execute(&fixture.root, "write_chunk", &args)),
                "invalid_path",
                "{data:?}"
            );
        }
        assert_eq!(names(&fixture.root), ["b.txt", "sub"]);
    }

    #[test]
    fn write_chunk_rejects_chunks_over_chunk_bytes() {
        let fixture = fixture("oversize-chunk");

        let error = chunk(&fixture.root, "x.bin", 0, &binary(CHUNK_BYTES as usize + 1))
            .expect_err("oversized chunk must fail");
        assert_eq!(error.code, "too_large");
        assert_eq!(names(&fixture.root), ["b.txt", "sub"]);
    }

    #[test]
    fn write_commit_requires_the_exact_size() {
        let fixture = fixture("commit-size");
        let temp = part(&fixture.root, "", "datos.bin");

        assert_eq!(
            code(commit(&fixture.root, "datos.bin", 0)),
            "io_error",
            "a commit without an upload must fail"
        );

        chunk(&fixture.root, "datos.bin", 0, b"abc").expect("chunk");
        for size in [2, 4] {
            assert_eq!(
                code(commit(&fixture.root, "datos.bin", size)),
                "io_error",
                "size={size}"
            );
            assert!(!fixture.root.join("datos.bin").exists());
            assert_eq!(fs::read(&temp).expect("temp"), b"abc", "the temp is kept");
        }

        commit(&fixture.root, "datos.bin", 3).expect("exact size must commit");
        assert_eq!(
            fs::read(fixture.root.join("datos.bin")).expect("file"),
            b"abc"
        );
    }

    #[test]
    fn write_commit_refuses_a_destination_created_during_the_upload() {
        let fixture = fixture("commit-race");
        let temp = part(&fixture.root, "sub", "datos.bin");

        chunk(&fixture.root, "sub/datos.bin", 0, b"subido").expect("chunk");
        fs::write(fixture.root.join("sub/datos.bin"), b"otro").expect("fixture");

        let error = commit(&fixture.root, "sub/datos.bin", 6).expect_err("must fail");
        assert_eq!(error, OpError::already_exists());
        assert_eq!(
            fs::read(fixture.root.join("sub/datos.bin")).expect("file"),
            b"otro"
        );
        assert_eq!(fs::read(&temp).expect("temp"), b"subido");
    }

    #[test]
    fn write_abort_removes_the_temp_file_and_is_idempotent() {
        let fixture = fixture("abort");
        let temp = part(&fixture.root, "sub", "datos.bin");

        chunk(&fixture.root, "sub/datos.bin", 0, b"parcial").expect("chunk");
        assert!(temp.exists());

        assert_eq!(
            abort(&fixture.root, "sub/datos.bin").expect("abort"),
            json!({"path": "sub/datos.bin", "removed": true})
        );
        assert!(!temp.exists());
        assert_eq!(
            abort(&fixture.root, "sub/datos.bin").expect("second abort"),
            json!({"path": "sub/datos.bin", "removed": false})
        );
        assert_eq!(names(&fixture.root.join("sub")), Vec::<String>::new());

        assert_eq!(
            abort(&fixture.root, "b.txt").expect("abort without upload"),
            json!({"path": "b.txt", "removed": false})
        );
        assert_eq!(
            fs::read(fixture.root.join("b.txt")).expect("file"),
            b"bravo"
        );
    }

    #[test]
    fn existing_read_and_write_ops_keep_their_limits() {
        let fixture = fixture("existing-ops");
        let large = fs::File::create(fixture.root.join("grande.bin")).expect("fixture");
        large
            .set_len(MAX_READ_BYTES + 1)
            .expect("fixture must grow");

        let error = execute(
            &fixture.root,
            "read_file",
            &json!({"path": "grande.bin", "offset": 0, "length": 4}),
        )
        .expect_err("read_file keeps its 5 MB cap");
        assert_eq!(
            error,
            OpError::new("too_large", "El archivo supera el límite de 5 MB.")
        );
        assert_eq!(
            read_chunk(&fixture.root, "grande.bin", MAX_READ_BYTES, 4).expect("chunk read")["size"],
            MAX_READ_BYTES + 1
        );

        assert_eq!(
            execute(
                &fixture.root,
                "write_file",
                &json!({"path": "sub/nota.txt", "content": "hola", "upload_id": ID}),
            )
            .expect("write_file still writes text"),
            json!({"path": "sub/nota.txt", "bytes": 4, "created": true})
        );
        assert_eq!(
            fs::read_to_string(fixture.root.join("sub/nota.txt")).expect("file"),
            "hola"
        );
        assert_eq!(names(&fixture.root.join("sub")), ["nota.txt"]);
    }
}
