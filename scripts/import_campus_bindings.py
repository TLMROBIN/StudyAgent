"""Preview or import reviewed campus-to-StudyAgent bindings from a private CSV."""
from argparse import ArgumentParser
import csv
from pathlib import Path

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from backend.config import get_settings
from backend.database import SessionLocal
from backend.models.user import CampusIdentity, User, UserRole


def import_bindings(db: Session, csv_path: Path, school_id: str, *, apply: bool) -> tuple[int, int]:
    planned = existing = 0
    seen_ids: set[int] = set()
    seen_numbers: set[str] = set()
    with csv_path.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        columns = {"StudyAgent用户ID", "StudyAgent账号", "确认的中台学号"}
        if not columns.issubset(reader.fieldnames or []):
            raise ValueError("对照表缺少必需的列")
        try:
            for row in reader:
                number = (row["确认的中台学号"] or "").strip()
                if not number:
                    continue
                try:
                    uid = int(row["StudyAgent用户ID"] or "")
                except ValueError:
                    raise ValueError("原账号 ID 格式无效") from None
                if uid in seen_ids or number in seen_numbers or len(number) > 64:
                    raise ValueError("重复绑定或学号格式无效")
                seen_ids.add(uid)
                seen_numbers.add(number)
                user = db.get(User, uid)
                if (
                    user is None
                    or user.username != row["StudyAgent账号"]
                    or user.role not in (UserRole.STUDENT, UserRole.TEACHER)
                    or not user.is_active
                    or user.graduated_at is not None
                ):
                    raise ValueError("原 StudyAgent 账号已变更或不可用")
                bindings = db.scalars(select(CampusIdentity).where(
                    CampusIdentity.school_id == school_id,
                    or_(CampusIdentity.number == number, CampusIdentity.user_id == uid),
                )).all()
                if bindings:
                    if len(bindings) != 1 or bindings[0].number != number or bindings[0].user_id != uid:
                        raise ValueError("此行与已保存的身份绑定冲突")
                    existing += 1
                else:
                    db.add(CampusIdentity(school_id=school_id, number=number, user_id=uid))
                    planned += 1
            if apply:
                db.commit()
            else:
                db.rollback()
        except ValueError as exc:
            db.rollback()
            raise ValueError(f"第 {reader.line_num} 行：{exc}") from None
    return planned, existing


def main() -> None:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("csv_file", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    school_id = get_settings().campus_sso_school_id
    if not school_id:
        parser.exit(2, "请先配置 CAMPUS_SSO_SCHOOL_ID\n")
    try:
        with SessionLocal() as session:
            planned, existing = import_bindings(session, args.csv_file, school_id, apply=args.apply)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"{exc}\n")
    print(f"{'已导入' if args.apply else '拟导入'} {planned} 条，已有 {existing} 条相同绑定。")


if __name__ == "__main__":
    main()
