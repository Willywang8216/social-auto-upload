#!/usr/bin/env python3
"""Copy only the allowlisted writing rules into new private versioned storage."""
from __future__ import annotations
import argparse
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from myUtils.repurpose.contracts import digest, json_bytes, require
from myUtils.repurpose.files import private_path, write_new
from myUtils.repurpose.writing import RULE_FILES, Rules


def bundle(source_root, output_root):
    # Installed skill aliases may resolve to their real installed tree. The
    # immutable output may not traverse aliases or enter a public repository.
    source = Path(source_root).resolve(strict=True)
    output = private_path(output_root)
    require(output.is_dir(), 'private_rules_parent_missing')
    payloads = {}
    for name in RULE_FILES:
        path = (source / name).resolve(strict=True)
        require(path.is_relative_to(source) and path.is_file() and path.stat().st_size <= 128 * 1024, 'installed_rule_path')
        payloads[name] = path.read_bytes()
    version = digest({name: hashlib.sha256(raw).hexdigest() for name, raw in payloads.items()})
    target = output / version
    if target.exists():
        rules = Rules(target)
        require(rules.version == version, 'existing_rules_bundle_changed')
        return target
    target.mkdir(mode=0o700)
    for name, raw in payloads.items():
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        write_new(path, raw)
    write_new(target / 'rules-manifest.json', json_bytes({'version': version, 'files': {name: hashlib.sha256(raw).hexdigest() for name, raw in payloads.items()}}))
    require(Rules(target).version == version, 'rules_bundle_readback')
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--skills-root', required=True)
    parser.add_argument('--out', required=True)
    args = parser.parse_args(argv)
    print(bundle(args.skills_root, args.out))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
