/** 技能包管理内容区（系统管理·技能包页签）。

左列技能包清单（题材知识包 / 模型提示词包两组），右侧全文编辑器
（含 frontmatter）：保存 = 快照上一版 → 写盘（解析/生成实时扫描目录，
保存即热生效，无需重启）；支持新建（占位模板）与删除。
 */

import { useEffect, useMemo, useState } from "react";
import {
  api,
  type SkillPackDetail,
  type SkillPackMeta,
} from "./api/client";
import { confirmDialog, promptDialog, toast } from "./ui/feedback";

/** 新建文件名输入提示（校验与后端一致：小写字母/数字开头） */
const FILENAME_HINT = "小写字母/数字开头，仅含小写字母数字下划线连字符，.md 结尾";
/** 文件名形态校验（与后端 SKILL_FILENAME_RE 对齐，前端先行拦截） */
const FILENAME_RE = /^[a-z0-9][a-z0-9_\-]{1,48}\.md$/;

export function SkillsContent() {
  const [items, setItems] = useState<SkillPackMeta[]>([]);
  const [active, setActive] = useState<string>("");
  const [detail, setDetail] = useState<SkillPackDetail | null>(null);
  const [draft, setDraft] = useState<string>("");
  const [saving, setSaving] = useState(false);
  const [creating, setCreating] = useState(false);

  /** 拉取清单（操作后统一刷新；prefer = 刷新后优先选中的文件；初始自动选中第一个） */
  const loadList = (prefer?: string) =>
    api.listSkills()
      .then((list) => {
        setItems(list);
        // 注意 ?? 不穿透空串：初始 active="" 需用 || 回退到首项
        setActive((cur) => prefer ?? (cur || list[0]?.file) ?? "");
      })
      .catch((e) => toast.error(`技能包清单加载失败：${e.message}`));

  // 选中条目变化 → 拉取全文
  useEffect(() => {
    if (!active) return;
    setDetail(null);
    api.getSkill(active)
      .then((d) => {
        setDetail(d);
        setDraft(d.content);
      })
      .catch((e) => toast.error(`技能包加载失败：${e.message}`));
  }, [active]);

  useEffect(() => {
    void loadList();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const dirty = detail !== null && draft !== detail.content;
  const meta = items.find((it) => it.file === active) ?? null;

  const onSave = async () => {
    if (!detail || !dirty) return;
    setSaving(true);
    try {
      await api.saveSkill(detail.file, draft);
      toast.success(`${detail.file} 已保存并热生效`);
      const d = await api.getSkill(detail.file);
      setDetail(d);
      setDraft(d.content);
      void loadList(detail.file); // 字数/描述可能变化
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  const onRestore = async () => {
    if (!detail?.has_snapshot) return;
    const ok = await confirmDialog({
      title: "恢复上一版",
      message: `将把 ${detail.file} 恢复到本次编辑前的快照版本，继续？`,
    });
    if (!ok) return;
    try {
      await api.restoreSkill(detail.file);
      toast.success(`${detail.file} 已恢复上一版`);
      const d = await api.getSkill(detail.file);
      setDetail(d);
      setDraft(d.content);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
    }
  };

  /** 新建技能包：输入文件名 → 写入占位模板 → 选中进入编辑 */
  const onCreate = async () => {
    const file = await promptDialog({
      title: "新建技能包",
      message: `输入文件名（${FILENAME_HINT}）。将创建占位模板，请编辑 frontmatter 与正文后保存。`,
      placeholder: "如：sci_fi_hard.md",
    });
    if (file === null) return;
    const name = file.trim().toLowerCase();
    if (!FILENAME_RE.test(name)) {
      toast.warning(`文件名不合法：${FILENAME_HINT}`);
      return;
    }
    setCreating(true);
    try {
      await api.createSkill(name);
      toast.success(`已创建 ${name}（占位模板，请编辑后保存）`);
      await loadList(name);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
    } finally {
      setCreating(false);
    }
  };

  const onDelete = async () => {
    if (!detail) return;
    const ok = await confirmDialog({
      title: "删除技能包",
      message: `确定删除 ${detail.file}？后续解析/生成将不再注入该技能包，且不可恢复。`,
      confirmText: "删除",
      danger: true,
    });
    if (!ok) return;
    try {
      await api.deleteSkill(detail.file);
      toast.success(`已删除 ${detail.file}`);
      setActive("");
      await loadList();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
    }
  };

  // 按 kind 分组（题材知识包 / 模型提示词包）
  const grouped = useMemo(() => {
    const genres = items.filter((it) => it.kind === "genre");
    const models = items.filter((it) => it.kind === "model");
    return [
      { key: "genre", title: "题材知识包", list: genres },
      { key: "model", title: "模型提示词包", list: models },
    ];
  }, [items]);

  return (
    <div className="prompts-wrap">
      <div className="prompts-layout">
        <div className="prompts-list">
          {grouped.map(({ key, title, list }) => (
            list.length > 0 && (
              <div key={key} className="prompts-group">
                <div className="prompts-group-title">{title}</div>
                {list.map((it) => (
                  <button
                    key={it.file}
                    className={`prompts-item${it.file === active ? " on" : ""}`}
                    onClick={() => setActive(it.file)}
                    title={it.description}
                  >
                    <span className="skill-item-name">{it.name}</span>
                    <span className="skill-item-applies">
                      {(it.models.length ? it.models : it.genres).slice(0, 3).join(" · ")}
                    </span>
                  </button>
                ))}
              </div>
            )
          ))}
          {!items.length && <div className="prompts-empty">暂无技能包</div>}
          <button
            className="prompt-btn skill-create"
            disabled={creating}
            onClick={() => void onCreate()}
          >
            {creating ? "创建中…" : "＋ 新建技能包"}
          </button>
        </div>
        <div className="prompts-editor">
          {detail && meta ? (
            <>
              <div className="prompts-editor-head">
                <span className="prompts-name" title={meta.description}>
                  {meta.name}
                  <span className="skill-kind-badge">
                    {meta.kind === "model" ? "模型包" : "题材包"}
                  </span>
                </span>
                <span className="prompts-actions">
                  {detail.has_snapshot && (
                    <button className="prompt-btn" onClick={onRestore}>
                      恢复上一版
                    </button>
                  )}
                  <button className="prompt-btn danger" onClick={onDelete}>
                    删除
                  </button>
                  <button
                    className="prompt-btn primary"
                    disabled={!dirty || saving}
                    onClick={onSave}
                  >
                    {saving ? "保存中…" : "保存（热生效）"}
                  </button>
                </span>
              </div>
              <div className="skill-meta-row">
                {meta.task_type && (
                  <span className="pc-tag plain">注入：{meta.task_type.toUpperCase()}</span>
                )}
                {meta.genres.length > 0 && (
                  <span className="pc-tag plain">题材：{meta.genres.join("、")}</span>
                )}
                {meta.models.length > 0 && (
                  <span className="pc-tag plain">适用模型：{meta.models.join("、")}</span>
                )}
                <span className="pc-tag plain">{meta.chars} 字</span>
              </div>
              <textarea
                className="prompts-textarea"
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                spellCheck={false}
              />
              <div className="prompts-footnote">
                首部 --- 块为 frontmatter（题材包声明 genres，模型包声明 models 与
                task_type）。保存即热生效：解析/生成每次实时扫描技能目录，无需重启。
              </div>
            </>
          ) : (
            <div className="prompts-empty">选择左侧技能包查看与编辑</div>
          )}
        </div>
      </div>
    </div>
  );
}
