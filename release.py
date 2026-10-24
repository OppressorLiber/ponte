"""Prepare and sign public release assets. Private keys never enter this tree."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from pontelan import __version__
from pontelan.update_helper import load_config
from pontelan.updates import verify_manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--notes-file", required=True, type=Path)
    parser.add_argument("--key", type=Path, default=Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) /
                        "Ponte" / "release-signing" / "ed25519-private.pem")
    args = parser.parse_args()
    config = load_config()
    key = serialization.load_pem_private_key(args.key.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise RuntimeError("A chave de assinatura precisa ser Ed25519.")
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    if base64.b64encode(public).decode() != config["public_key"]:
        raise RuntimeError("A chave privada não corresponde à chave pública já distribuída.")
    args.out.mkdir(parents=True, exist_ok=True)
    target = args.out / "Ponte.exe"
    if args.exe.absolute() != target.absolute():
        shutil.copy2(args.exe, target)
    digest = hashlib.file_digest(target.open("rb"), "sha256").hexdigest()
    manifest = {"schema": 1, "repository": config["repository"], "version": __version__,
                "notes": args.notes_file.read_text(encoding="utf-8-sig").strip(),
                "windows": {"url": f"https://github.com/{config['repository']}/releases/download/v{__version__}/Ponte.exe",
                            "size": target.stat().st_size, "sha256": digest}}
    raw = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    signature = key.sign(raw)
    verify_manifest(raw, signature, config, expected_version=__version__)
    (args.out / "update.json").write_bytes(raw)
    (args.out / "update.sig").write_text(base64.b64encode(signature).decode() + "\n", encoding="ascii")
    checksums = []
    for item in sorted(args.out.iterdir()):
        if item.is_file() and item.name != "SHA256SUMS.txt":
            with item.open("rb") as stream:
                checksums.append(hashlib.file_digest(stream, "sha256").hexdigest() + "  " + item.name)
    (args.out / "SHA256SUMS.txt").write_text("\n".join(checksums) + "\n", encoding="ascii")
    print(json.dumps({"version": __version__, "repository": config["repository"],
                      "size": target.stat().st_size, "sha256": digest}))


if __name__ == "__main__":
    main()
