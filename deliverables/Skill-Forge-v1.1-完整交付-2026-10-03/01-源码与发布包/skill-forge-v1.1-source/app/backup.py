"""备份/恢复 CLI（S7）。

用法（服务停止时执行）：

    uv run python -m app.backup create --out backup.zip
    uv run python -m app.backup restore backup.zip --data-dir data
    uv run python -m app.backup info backup.zip

数据目录取 config.yaml / SKILLFORGE_DATA_DIR 环境变量（同主程序）。
"""
import argparse
import sys
from pathlib import Path

from app.core.config import load_config
from app.services.backup import (
    BackupError,
    backup_info,
    create_backup,
    restore_backup,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.backup", description="skill-forge 备份/恢复")
    sub = parser.add_subparsers(dest="command", required=True)

    p_create = sub.add_parser("create", help="导出数据目录到 zip 备份")
    p_create.add_argument("--out", type=Path, required=True, help="备份包输出路径")

    p_restore = sub.add_parser("restore", help="恢复备份到空数据目录")
    p_restore.add_argument("backup", type=Path, help="备份包路径")
    p_restore.add_argument(
        "--data-dir", type=Path, default=None,
        help="恢复目标数据目录（默认取配置，必须为空）",
    )

    p_info = sub.add_parser("info", help="查看备份信息")
    p_info.add_argument("backup", type=Path, help="备份包路径")

    args = parser.parse_args(argv)

    try:
        if args.command == "create":
            cfg = load_config()
            manifest = create_backup(cfg, args.out)
            print(f"备份完成: {args.out}")
            print(f"  schema 版本: {manifest['schema_version']}")
            print(f"  归档文件数: {manifest['file_count']}")
            print(f"  创建时间:   {manifest['created_at']}")
        elif args.command == "restore":
            data_dir = args.data_dir
            if data_dir is None:
                data_dir = load_config().data_dir
            manifest = restore_backup(args.backup, Path(data_dir))
            print(f"恢复完成: {data_dir}")
            print(f"  备份创建于: {manifest['created_at']}（schema {manifest['schema_version']}）")
            print("  如备份 schema 旧于当前版本，请执行: uv run alembic upgrade head")
        elif args.command == "info":
            manifest = backup_info(args.backup)
            entries = manifest.pop("entries", [])
            for k, v in manifest.items():
                print(f"  {k}: {v}")
            print(f"  条目数: {len(entries)}")
        return 0
    except BackupError as err:
        print(f"错误: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
