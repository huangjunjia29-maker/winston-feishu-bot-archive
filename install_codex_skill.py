"""Install Winston's global Codex skill for this checkout, without credentials."""
import argparse
import os
import shutil
from datetime import datetime
from pathlib import Path


def install(project_root, codex_dir):
    project_root, codex_dir = project_root.resolve(), codex_dir.resolve()
    source = project_root / 'codex/winston-feishu'
    target = codex_dir / 'skills/winston-feishu'
    guide = codex_dir / 'AGENTS.md'
    start, end = '<!-- winston-feishu:start -->', '<!-- winston-feishu:end -->'
    existing = guide.read_text(encoding='utf-8-sig') if guide.exists() else ''
    if existing.count(start) != existing.count(end) or existing.count(start) > 1:
        raise RuntimeError('Ambiguous Winston markers; inspect AGENTS.md before updating.')
    block = (f'{start}\n# Winston / 温斯顿飞书入口\n\n'
             f'当用户调用温斯顿或处理飞书任务时，读取 `{(target / "SKILL.md").as_posix()}`。\n'
             f'复用项目 `{project_root.as_posix()}` 的认证模块和本机环境。\n'
             '凭证由运行时代码读取，不复制、打印或提交。读取实时数据，限定写入范围并回读验证；已授权的操作不重复询问。\n'
             f'此技能不共享聊天历史，不新增飞书权限。\n{end}')
    if start in existing:
        left, right = existing.index(start), existing.index(end)
        if right < left:
            raise RuntimeError('Invalid Winston marker order.')
        updated = existing[:left] + block + existing[right + len(end):]
    else:
        updated = existing + ('\n\n' if existing else '') + block + '\n'
    rendered = {}
    for relative in ('SKILL.md', 'scripts/check_runtime.py'):
        value = (source / relative).read_text(encoding='utf-8')
        rendered[relative] = value.replace('__WINSTON_PROJECT_ROOT_LITERAL__', repr(project_root.as_posix())).replace('__WINSTON_PROJECT_ROOT__', project_root.as_posix())
    if target.exists() or guide.exists():
        backup = codex_dir / 'winston-install-backups' / datetime.now().strftime('%Y%m%d-%H%M%S-%f')
        backup.mkdir(parents=True)
        if target.exists():
            shutil.copytree(target, backup / 'winston-feishu')
        if guide.exists():
            shutil.copy2(guide, backup / 'AGENTS.md')
    for relative, value in rendered.items():
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(value, encoding='utf-8')
    guide.write_text(updated, encoding='utf-8')
    print('Installed Winston skill:', target)
    print('Updated global guide:', guide)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--codex-dir', type=Path, default=Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex'))
    args = parser.parse_args()
    install(Path(__file__).resolve().parent, args.codex_dir)
