"""独立静态验证与完整性报告（工程计划 §5 T09）。

纪律：验证器只信磁盘与字节，不信模型自报——
- manifest 提供"该验什么"（路径/哈希/资源引用）；
- 磁盘提供"实际是什么"（读文件、算哈希、解析语法）；
- 模型自称通过（哪怕写进 manifest）不构成任何证据。

检查器约定（check_id = 规则 checker_id）：
- validation:skill_header     SKILL.md 头信息（YAML front matter 含 name）；
- validation:script_syntax    .py 真实 ast 解析；.sh/.ps1 仅启发式冒号配对检查；
- validation:binary_integrity 二进制存在 + sha256 与 manifest 一致；
- validation:skill_size       行数超 profile config.max_lines 报 failed（severity 由调用方判）；
- validation:all_files        清单文件逐一存在且哈希一致（缺文件/hash 错 → failed）。

无 checker_id 的纯语义规则 → unsupported（诚实报告能力边界）。
"""
from __future__ import annotations

import ast
import re
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

from app.generation.contracts import (
    ArtifactKind,
    ArtifactManifest,
    CheckReport,
    CheckResult,
    CheckStatus,
    EngineeringProfile,
    RequirementContract,
)

_SCRIPT_EXTS = {".py", ".sh", ".ps1"}
_BINARY_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".svg"}


def _hash_file(path: Path) -> str | None:
    try:
        return sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _check_skill_header(root: Path, manifest: ArtifactManifest) -> CheckResult:
    """SKILL.md 头信息：文件存在 + YAML front matter 含 name。"""
    skill = root / "SKILL.md"
    if not skill.is_file():
        return CheckResult(
            check_id="validation:skill_header", status=CheckStatus.FAILED,
            evidence="SKILL.md 不存在",
        )
    text = skill.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return CheckResult(
            check_id="validation:skill_header", status=CheckStatus.FAILED,
            evidence="SKILL.md 缺少 YAML front matter（应以 --- 开头）",
        )
    end = text.find("\n---", 3)
    if end == -1:
        return CheckResult(
            check_id="validation:skill_header", status=CheckStatus.FAILED,
            evidence="front matter 未闭合（缺少结束 ---）",
        )
    header = text[3:end]
    has_name = any(
        line.strip().startswith("name:") for line in header.splitlines()
    )
    if not has_name:
        return CheckResult(
            check_id="validation:skill_header", status=CheckStatus.FAILED,
            evidence="front matter 缺少 name 字段",
        )
    return CheckResult(
        check_id="validation:skill_header", status=CheckStatus.PASSED,
        evidence="front matter 含 name 字段",
        artifact_hash=_hash_file(skill) or "",
    )


def _check_script_syntax(root: Path, manifest: ArtifactManifest) -> CheckResult:
    """脚本语法：.py 用 ast 真实解析；.sh/.ps1 冒号配对启发式。"""
    scripts = [a for a in manifest.artifacts if a.path.startswith("scripts/")]
    if not scripts:
        return CheckResult(
            check_id="validation:script_syntax", status=CheckStatus.PASSED,
            evidence="无脚本文件（纯文本技能）",
        )
    errors: list[str] = []
    for art in scripts:
        f = root / art.path
        if not f.is_file():
            errors.append(f"{art.path}: 文件缺失")
            continue
        if f.suffix == ".py":
            source = f.read_text(encoding="utf-8")
            try:
                ast.parse(source, filename=str(art.path))
            except SyntaxError as exc:
                errors.append(f"{art.path}: 语法错误 {exc.msg}（第 {exc.lineno} 行）")
        elif f.suffix in (".sh", ".ps1"):
            # 启发式：括号/引号粗配对（无 shell 解析器时的诚实降级）
            text = f.read_text(encoding="utf-8")
            if text.count("(") != text.count(")"):
                errors.append(f"{art.path}: 括号不配对（启发式检查）")
    if errors:
        return CheckResult(
            check_id="validation:script_syntax", status=CheckStatus.FAILED,
            evidence="; ".join(errors),
        )
    return CheckResult(
        check_id="validation:script_syntax", status=CheckStatus.PASSED,
        evidence=f"{len(scripts)} 个脚本语法通过",
    )


def _check_binary_integrity(root: Path, manifest: ArtifactManifest) -> CheckResult:
    """二进制完整性：存在 + sha256 与 manifest 一致（不能用文本解码冒充）。"""
    binaries = [
        a for a in manifest.artifacts
        if a.kind == ArtifactKind.BINARY or Path(a.path).suffix in _BINARY_EXTS
    ]
    if not binaries:
        return CheckResult(
            check_id="validation:binary_integrity", status=CheckStatus.PASSED,
            evidence="无二进制资源",
        )
    errors: list[str] = []
    for art in binaries:
        f = root / art.path
        if not f.is_file():
            errors.append(f"{art.path}: 二进制文件缺失")
            continue
        actual = sha256(f.read_bytes()).hexdigest()
        if actual != art.sha256:
            errors.append(f"{art.path}: 哈希不符（声明 {art.sha256[:12]}… 实际 {actual[:12]}…）")
    if errors:
        return CheckResult(
            check_id="validation:binary_integrity", status=CheckStatus.FAILED,
            evidence="; ".join(errors),
        )
    return CheckResult(
        check_id="validation:binary_integrity", status=CheckStatus.PASSED,
        evidence=f"{len(binaries)} 个二进制资源哈希一致",
    )


def _check_all_files(root: Path, manifest: ArtifactManifest) -> CheckResult:
    """清单完整性：每个声明文件存在且哈希一致（缺文件/hash 错 → failed）。"""
    errors: list[str] = []
    for art in manifest.artifacts:
        f = root / art.path
        if not f.is_file():
            errors.append(f"{art.path}: 声明但磁盘缺失")
            continue
        actual = sha256(f.read_bytes()).hexdigest()
        if actual != art.sha256:
            errors.append(f"{art.path}: 哈希不符")
    if errors:
        return CheckResult(
            check_id="validation:all_files", status=CheckStatus.FAILED,
            evidence="; ".join(errors),
        )
    return CheckResult(
        check_id="validation:all_files", status=CheckStatus.PASSED,
        evidence=f"{len(manifest.artifacts)} 个文件完整",
    )


def _check_skill_size(root: Path, manifest: ArtifactManifest, max_lines: int) -> CheckResult:
    """行数建议检查：超限如实报 failed（severity=warning，是否阻塞由调用方判）。"""
    skill = root / "SKILL.md"
    if not skill.is_file():
        return CheckResult(
            check_id="validation:skill_size", status=CheckStatus.FAILED,
            evidence="SKILL.md 不存在",
        )
    lines = skill.read_text(encoding="utf-8").count("\n") + 1
    if lines > max_lines:
        return CheckResult(
            check_id="validation:skill_size", status=CheckStatus.FAILED,
            evidence=f"SKILL.md {lines} 行超过建议上限 {max_lines} 行（warning 级）",
        )
    return CheckResult(
        check_id="validation:skill_size", status=CheckStatus.PASSED,
        evidence=f"SKILL.md {lines} 行（上限 {max_lines}）",
    )


_CHECKER_MAP = {
    "validation:skill_header": lambda root, m, c, r, cfg: _check_skill_header(root, m),
    "validation:script_syntax": lambda root, m, c, r, cfg: _check_script_syntax(root, m),
    "validation:binary_integrity": lambda root, m, c, r, cfg: _check_binary_integrity(root, m),
    "validation:skill_size": lambda root, m, c, r, cfg: _check_skill_size(
        root, m, int(cfg.get("max_lines", 500))
    ),
    # v1.3：三个真实价值检查器（详见 docs/v1.3修改建议.md 砍后清单）
    "validation:no_secrets": lambda root, m, c, r, cfg: _check_no_secrets(root, m),
    "validation:file_hash": lambda root, m, c, r, cfg: _check_file_hash(root, m),
    "execution:script": lambda root, m, c, r, cfg: _check_script_execution(
        root, m, float(cfg.get("timeout_seconds", 10))
    ),
    # v1.3 补齐：profile 声明的最后两个检查器
    "validation:resource_reference": lambda root, m, c, r, cfg: _check_resource_reference(root, m),
    "coverage:requirements": lambda root, m, c, r, cfg: _check_requirements_coverage(m, c),
}


# v1.3：与打包侧共用的密钥标记（delivery.py 同源，双体系对齐）
_SECRET_MARKERS = ("SKILLFORGE_TOKEN", "API_KEY=", "SECRET_TOKEN", "api_key =")

# SKILL.md 正文中引用的产物路径（v1.3 resource_reference）
_REF_DIR_PATTERN = re.compile(
    r"(?:references|scripts|assets)/[\w\-.\u4e00-\u9fff]+(?:\.[\w\-]+)?"
)


def _check_resource_reference(root: Path, manifest: ArtifactManifest) -> CheckResult:
    """SKILL.md 引用的产物路径必须存在于清单（规则 RESOURCE_REFERENCE）。

    只信磁盘与清单：扫 SKILL.md 正文中的 references/ scripts/ assets/
    路径字面量，逐一比对 manifest 声明（防止“文档说有但清单没有”）。反向
    不查：清单里未被引用的文件不构成错误。
    """
    skill = root / "SKILL.md"
    if not skill.is_file():
        return CheckResult(
            check_id="validation:resource_reference", status=CheckStatus.FAILED,
            evidence="SKILL.md 不存在",
        )
    declared = {a.path for a in manifest.artifacts}
    referenced = sorted(set(_REF_DIR_PATTERN.findall(skill.read_text(encoding="utf-8"))))
    if not referenced:
        return CheckResult(
            check_id="validation:resource_reference", status=CheckStatus.PASSED,
            evidence="SKILL.md 未引用清单类目录文件",
        )
    missing = [p for p in referenced if p not in declared]
    if missing:
        return CheckResult(
            check_id="validation:resource_reference", status=CheckStatus.FAILED,
            evidence="SKILL.md 引用但清单缺失: " + ", ".join(missing),
        )
    return CheckResult(
        check_id="validation:resource_reference", status=CheckStatus.PASSED,
        evidence=f"{len(referenced)} 个引用均在清单内",
    )


def _check_requirements_coverage(
    manifest: ArtifactManifest, contract: RequirementContract
) -> CheckResult:
    """必要需求必须映射到至少一个产物文件（规则 COVERAGE 的静态聚合）。

    与 coverage.py 的逐需求 evaluate_coverage 互补：这里只做静态可判的
    “有无实现位置”（missing 判定，不需要 plan）；verified 级证明仍由
    逐需求覆盖报告承担——只写映射不能证明实现。
    """
    missing = [
        r.requirement_id for r in contract.requirements
        if r.necessary and not any(
            r.requirement_id in a.requirement_ids for a in manifest.artifacts
        )
    ]
    if missing:
        return CheckResult(
            check_id="coverage:requirements", status=CheckStatus.FAILED,
            evidence="必要需求未映射到任何产物文件: " + ", ".join(missing),
        )
    necessary = [r for r in contract.requirements if r.necessary]
    return CheckResult(
        check_id="coverage:requirements", status=CheckStatus.PASSED,
        evidence=f"{len(necessary)} 项必要需求均有产物映射（验收证据由逐需求覆盖报告判定）",
    )


def _check_no_secrets(root: Path, manifest: ArtifactManifest) -> CheckResult:
    """密钥内容扫描：复用打包侧 _scan_for_secrets 的同一套标记。

    验证阶段扫候选文件（提前拦），打包阶段再扫一次（最后克防线）——
    同一标记集，双阶段同语义。
    """
    hits: list[str] = []
    for art in manifest.artifacts:
        f = root / art.path
        if not f.is_file():
            continue  # 缺文件由 all_files / file_hash 报，不重复报
        try:
            text = f.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            continue  # 二进制不扫
        for marker in _SECRET_MARKERS:
            if marker in text:
                hits.append(f"{art.path}（{marker}）")
                break
    if hits:
        return CheckResult(
            check_id="validation:no_secrets", status=CheckStatus.FAILED,
            evidence="疑似密钥内容: " + "; ".join(hits),
        )
    return CheckResult(
        check_id="validation:no_secrets", status=CheckStatus.PASSED,
        evidence=f"{len(manifest.artifacts)} 个文件无密钥标记",
    )


def _check_file_hash(root: Path, manifest: ArtifactManifest) -> CheckResult:
    """全部产物磁盘实测哈希 vs manifest 声明（含文本，补 all_files 之外的细粒度）。"""
    errors: list[str] = []
    for art in manifest.artifacts:
        f = root / art.path
        if not f.is_file():
            errors.append(f"{art.path}: 磁盘缺失")
            continue
        actual = sha256(f.read_bytes()).hexdigest()
        if actual != art.sha256:
            errors.append(f"{art.path}: 哈希不符（磁盘 {actual[:12]}）")
    if errors:
        return CheckResult(
            check_id="validation:file_hash", status=CheckStatus.FAILED,
            evidence="; ".join(errors),
        )
    return CheckResult(
        check_id="validation:file_hash", status=CheckStatus.PASSED,
        evidence=f"{len(manifest.artifacts)} 个文件哈希一致",
    )


_SCRIPT_EXTS = {".py", ".sh", ".ps1"}


def _check_script_execution(
    root: Path, manifest: ArtifactManifest, timeout: float
) -> CheckResult:
    """执行维度的最小实现：脚本空输入 smoke（v1.3 砍后清单 P1-3）。

    - 只跑项目产物目录内的脚本（沙箱路径层约束，同现有声明）；
    - 空输入调用：脚本应当能优雅处理无输入（读 stdin 结束/空参数），
      崩溃/超时如实 FAILED——不假装跑过；
    - 命令注入面：路径来自 manifest 且已在磁盘哈希验证范围内。
    """
    scripts = [a for a in manifest.artifacts if Path(a.path).suffix in _SCRIPT_EXTS]
    if not scripts:
        return CheckResult(
            check_id="execution:script", status=CheckStatus.PASSED,
            evidence="无脚本可执行",
        )
    errors: list[str] = []
    ok: list[str] = []
    for art in scripts:
        f = root / art.path
        if not f.is_file():
            errors.append(f"{art.path}: 磁盘缺失")
            continue
        if art.path.endswith(".py"):
            cmd = [sys.executable, str(f)]
        elif art.path.endswith(".sh"):
            cmd = ["bash", str(f)]
        else:
            cmd = ["pwsh", "-NoProfile", "-File", str(f)]
        try:
            proc = subprocess.run(
                cmd, input="", capture_output=True, text=True,
                timeout=timeout, cwd=str(root),
            )
        except subprocess.TimeoutExpired:
            errors.append(f"{art.path}: 空输入下超时（>{timeout:.0f}s）——"
                          "脚本可能阻塞等待输入，未能优雅处理")
            continue
        except OSError as exc:
            errors.append(f"{art.path}: 无法执行（{exc}）")
            continue
        # 空输入下退出码非 0 → 脚本未优雅处理无输入场景，如实报告
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            tail = detail[-1][:80] if detail else "无输出"
            errors.append(f"{art.path}: 空输入下退出码 {proc.returncode}（{tail}）")
        else:
            ok.append(art.path)
    if errors:
        return CheckResult(
            check_id="execution:script", status=CheckStatus.FAILED,
            evidence="; ".join(errors),
        )
    return CheckResult(
        check_id="execution:script", status=CheckStatus.PASSED,
        evidence=f"{len(ok)} 个脚本空输入 smoke 通过（{', '.join(ok)}）",
    )


def validate_candidate(
    manifest: ArtifactManifest,
    contract: RequirementContract,
    profile: EngineeringProfile,
    root: Path,
) -> CheckReport:
    """对候选目录做独立静态验证，产出完整性报告。

    root 是磁盘上的候选/暂存目录；manifest 只提供声明（该验什么），
    结论一律来自磁盘实测。规则与检查器的映射：
    - 每条 blocking 规则必须有对应检查执行（缺文件/哈希错必 failed）；
    - 无 checker_id 的纯语义规则 → unsupported；
    - 通用完整性检查（validation:all_files）始终执行。
    """
    checks: list[CheckResult] = [
        _check_all_files(root, manifest),
    ]

    for rule in profile.rules:
        if not rule.checker_id:
            # 纯语义规则：静态验证器无能力，诚实报告 unsupported
            checks.append(CheckResult(
                check_id=f"rule:{rule.rule_id}", status=CheckStatus.UNSUPPORTED,
                evidence=f"规则 {rule.rule_id} 为纯语义规则，静态检查器不适用",
            ))
            continue

        checker = _CHECKER_MAP.get(rule.checker_id)
        if checker is None:
            checks.append(CheckResult(
                check_id=rule.checker_id, status=CheckStatus.NOT_RUN,
                evidence=f"检查器 {rule.checker_id} 未注册（规则 {rule.rule_id}）",
            ))
            continue
        checks.append(checker(root, manifest, contract, rule, rule.config))

    return CheckReport(checks=checks)
