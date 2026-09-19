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

def model_dir(fs_root, prototype_id, model_id):
    return os.path.join(fs_root, str(prototype_id), "model", str(model_id))
