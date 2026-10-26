"""Sabit Hugging Face revision'larını repo dışına indir ve SHA-256 ile doğrula."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

DATASETS = (
    ("yagmurtuncer/turkish-text-normalization", "b4a9a5518260", "CC BY 4.0", "cc-by-4.0"),
    ("GoktugD/turkish-number-words-1m", "fdec892eebeb", "CC0", "cc0-1.0"),
    ("cloud0day3/alania-domain-text-tr", "20fd903fe656", "CC BY 4.0", "cc-by-4.0"),
)
REPO_ROOT = Path(__file__).resolve().parents[2]


def external_dir(value: str | Path) -> Path:
    path = Path(value).resolve()
    if path == REPO_ROOT or REPO_ROOT in path.parents:
        raise ValueError("Veri ve sonuç dizini repo dışında olmalıdır.")
    path.mkdir(parents=True, exist_ok=True)
    return path


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def open_url(url: str):
    return urllib.request.urlopen(
        urllib.request.Request(url, headers={"User-Agent": "katip-tn-eval/1.0"}), timeout=120
    )


def download(url: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".part")
    for attempt in range(3):
        try:
            with open_url(url) as response, temporary.open("wb") as stream:
                shutil.copyfileobj(response, stream, length=1024 * 1024)
            temporary.replace(target)
            return
        except (OSError, TimeoutError):
            temporary.unlink(missing_ok=True)
            if attempt == 2:
                raise
            time.sleep(2 ** (attempt + 1))


def verify_manifest(root: Path, manifest: dict) -> None:
    if manifest.get("schema_version") != 1:
        raise ValueError("Bilinmeyen manifest sürümü.")
    entries = {entry["id"]: entry for entry in manifest["datasets"]}
    if set(entries) != {item[0] for item in DATASETS}:
        raise ValueError("Manifest beklenen üç veri kümesini içermiyor.")
    for dataset, prefix, license_name, _ in DATASETS:
        entry = entries[dataset]
        if not entry["revision"].startswith(prefix) or len(entry["revision"]) != 40:
            raise ValueError(f"Revision uyuşmuyor: {dataset}")
        if entry["license"] != license_name or not entry["files"]:
            raise ValueError(f"Lisans/dosya listesi uyuşmuyor: {dataset}")
        for file in entry["files"]:
            path = (root / file["path"]).resolve()
            if root not in path.parents or not path.is_file():
                raise ValueError(f"Dosya bulunamadı veya dışarı taşıyor: {file['path']}")
            if path.stat().st_size != file["bytes"] or sha256(path) != file["sha256"]:
                raise ValueError(f"SHA-256 uyuşmuyor: {path}")


def fetch(root: Path) -> dict:
    manifest_path = root / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text("utf-8"))
        verify_manifest(root, manifest)
        print("Manifest ve tüm dosya hash'leri doğrulandı.", flush=True)
        return manifest
    manifest = {"schema_version": 1, "datasets": []}
    for dataset, prefix, license_name, license_id in DATASETS:
        with open_url(f"https://huggingface.co/api/datasets/{dataset}/revision/{prefix}") as r:
            info = json.load(r)
        revision = info["sha"]
        if not revision.startswith(prefix) or len(revision) != 40:
            raise ValueError(f"HF beklenen revision'ı çözümlemedi: {dataset}")
        if info.get("cardData", {}).get("license") != license_id:
            raise ValueError(f"HF lisansı beklenen değer değil: {dataset}")
        entry = {"id": dataset, "revision": revision, "license": license_name, "files": []}
        for item in sorted(info["siblings"], key=lambda f: f["rfilename"]):
            name = item["rfilename"]
            if not (
                name.startswith("data/")
                and name.endswith((".csv", ".parquet", ".json", ".jsonl"))
                or name
                in {"README.md", "LICENSE", "LICENSE.md", "DATASHEET.md", "QUALITY_REPORT.md"}
            ):
                continue
            path = (root / "datasets" / dataset / name).resolve()
            if root not in path.parents:
                raise ValueError("HF dosya yolu veri dizini dışına taşıyor.")
            url = f"https://huggingface.co/datasets/{dataset}/resolve/{revision}/{quote(name)}"
            print(f"İndiriliyor: {dataset}/{name}", flush=True)
            download(url, path)
            entry["files"].append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "source_file": name,
                    "url": url,
                    "sha256": sha256(path),
                    "bytes": path.stat().st_size,
                    "download_time": datetime.now(UTC).isoformat(),
                }
            )
        manifest["datasets"].append(entry)
    verify_manifest(root, manifest)
    temporary = manifest_path.with_suffix(".json.part")
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", "utf-8")
    temporary.replace(manifest_path)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True, type=external_dir)
    args = parser.parse_args()
    fetch(args.data_dir)


if __name__ == "__main__":
    main()
