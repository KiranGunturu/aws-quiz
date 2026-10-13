#!/usr/bin/env python3
"""Copy official AWS Architecture Icons into content/icons/<service-id>.svg.

1. Download the "Architecture Icons" asset package from https://aws.amazon.com/architecture/icons/
   and unzip it anywhere.
2. python tools/import_icons.py "C:\\path\\to\\Asset-Package"

Services without a match (or not AWS services, like Terraform) keep their letter tile on the site.
Edit ICON_FOR below to change which icon a service uses. Check AWS's terms for the icons before
publishing them on a public site.
"""
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "content" / "icons"

# service id -> distinctive part of the official file name (Arch_<this>_48.svg)
ICON_FOR = {
    "lambda": "AWS-Lambda",
    "ec2": "Amazon-EC2",
    "s3": "Amazon-Simple-Storage-Service",
    "ebs": "Amazon-Elastic-Block-Store",
    "api-gateway": "Amazon-API-Gateway",
    "cloudfront": "Amazon-CloudFront",
    "route53": "Amazon-Route-53",
    "networking": "Amazon-Virtual-Private-Cloud",
    "sqs": "Amazon-Simple-Queue-Service",
    "sns": "Amazon-Simple-Notification-Service",
    "ssl-tls": "AWS-Certificate-Manager",
    "app-security": "AWS-WAF",
    "ha-scalability": "Elastic-Load-Balancing",
    "dynamodb": "Amazon-DynamoDB",
}


def find(package: Path, key: str) -> Path | None:
    """Prefer the 48px SVG service icon (Arch_<key>_48.svg); fall back to any size."""
    pattern = re.compile(rf"^Arch_{re.escape(key)}_(\d+)\.svg$", re.I)
    hits = []
    # only the service-icon folder (thousands of other files make a full search slow), skip macOS junk
    roots = [d for d in package.glob("Architecture-Service-Icons*") if d.is_dir()] or [package]
    for p in (f for r in roots for f in r.rglob("Arch_*.svg") if "__MACOSX" not in f.parts):
        m = pattern.match(p.name)
        if m:
            hits.append((abs(int(m.group(1)) - 48), p))
    return min(hits)[1] if hits else None


def main() -> int:
    if len(sys.argv) != 2 or not Path(sys.argv[1]).is_dir():
        print(__doc__)
        return 2
    package = Path(sys.argv[1])
    OUT.mkdir(parents=True, exist_ok=True)
    missing = []
    for sid, key in ICON_FOR.items():
        src = find(package, key)
        if src is None:
            missing.append(f"{sid} (looked for Arch_{key}_48.svg)")
            continue
        shutil.copyfile(src, OUT / f"{sid}.svg")
        print(f"  {sid:<16} <- {src.name}")
    print(f"\n{len(ICON_FOR) - len(missing)} icon(s) copied to {OUT.relative_to(ROOT)}")
    if missing:
        print("not found (these keep their letter tile; adjust ICON_FOR if the file name changed):")
        for m in missing:
            print(f"  - {m}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
