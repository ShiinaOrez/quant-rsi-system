"""Fallback for official qwen3:8b registry downloads; validate every blob."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess


def digest_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch(root: Path):
    registry = "https://registry.ollama.ai/v2/library/qwen3"
    manifest = subprocess.check_output([
        "curl", "--fail", "--location", "--silent", "--show-error", "--retry", "2",
        "--header", "Accept: application/vnd.docker.distribution.manifest.v2+json",
        registry + "/manifests/8b",
    ])
    value = json.loads(manifest)
    blobs = root / "blobs"
    blobs.mkdir(parents=True, exist_ok=True)
    for layer in [value["config"]] + value["layers"]:
        algorithm, digest = layer["digest"].split(":")
        if algorithm != "sha256" or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("invalid registry content digest")
        target = blobs / ("sha256-" + digest)
        if not target.exists():
            partial = target.with_suffix(".part")
            subprocess.run([
                "curl", "--fail", "--location", "--silent", "--show-error", "--retry", "2",
                registry + "/blobs/" + layer["digest"], "--output", str(partial),
            ], check=True)
            if partial.stat().st_size != layer["size"] or digest_file(partial) != digest:
                raise ValueError("downloaded model integrity failure")
            os.replace(partial, target)
        elif target.stat().st_size != layer["size"] or digest_file(target) != digest:
            raise ValueError("existing model integrity failure")
        print(json.dumps({"verified_sha256": digest, "bytes": layer["size"]}), flush=True)
    target = root / "manifests/registry.ollama.ai/library/qwen3/8b"
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".part")
    temporary.write_bytes(manifest)
    os.replace(temporary, target)
    print(json.dumps({"model": "qwen3:8b", "manifest_sha256": hashlib.sha256(manifest).hexdigest()}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-root", type=Path, default=Path.home() / ".ollama/models")
    fetch(parser.parse_args().model_root)
