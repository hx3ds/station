import fcntl
import os
import re

_SAFE_PATH_COMPONENT = re.compile(r"[^A-Za-z0-9._-]+")

def sanitize_path_component(value, *, empty="item"):
    cleaned = _SAFE_PATH_COMPONENT.sub("_", value)
    cleaned = cleaned.strip("._")
    return cleaned or empty

def write_atomic(path, content, *, mode=None):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = "%s.tmp" % path
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, path)

def prototype_dir(fs_root, prototype_id):
    return os.path.join(fs_root, str(prototype_id))

def prototype_storage_dir(fs_root, prototype_id):
    return os.path.join(prototype_dir(fs_root, prototype_id), "storage")

def model_dir(fs_root, prototype_id, model_id):
    return os.path.join(prototype_dir(fs_root, prototype_id), "model", str(model_id))

def prototype_lock_path(fs_root, prototype_id):
    return os.path.join(prototype_dir(fs_root, prototype_id), ".prototype.lock")

def resolve_under_prototype(fs_root, prototype_id, path):

    from station.prototypes.boundary import ext_str

    path = ext_str("path", path)
    if not path:
        raise ValueError("path is required")
    if os.path.isabs(path):
        return path
    return os.path.join(prototype_dir(fs_root, prototype_id), path)

def acquire_file_lock(path):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    fd = open(path, "a+")
    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX)
    except OSError:
        fd.close()
        raise
    return fd

def release_file_lock(fd):
    if fd is None:
        return
    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
    finally:
        fd.close()
