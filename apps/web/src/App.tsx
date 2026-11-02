/** 应用入口：hash 路由分发（首页 / 登录 / 账号管理 / 项目工作台）。

路由化后刷新任意子页面均按 URL 原地恢复（项目元信息按 ID 重拉），
不再退回首页。未登录一律落在登录页；管理员才可见系统设置与账号管理入口。
 */

import { useEffect, useMemo, useState } from "react";
import {
  api,
  type ProjectInfo,
} from "./api/client";
import {
  SIGNED_URLS_READY,
  clearSignedUrls,
} from "./api/fileUrls";
import { CanvasView } from "./canvas/CanvasView";
import { DramaView, styleLabel } from "./drama/DramaView";
import { LoginPage } from "./pages/LoginPage";
import { ForcePasswordPage } from "./pages/ForcePasswordPage";
import { NewProjectDialog } from "./pages/NewProjectDialog";
import { CreditsPage } from "./pages/CreditsPage";
import { SystemPage } from "./pages/SystemPage";
import { BrandMark } from "./ui/BrandMark";
import { CreditBalance } from "./ui/CreditBalance";
import { useCanvas } from "./stores/canvas";
import { useAuth, rememberRedirect } from "./stores/auth";
import { useDrama } from "./stores/drama";
// 计费 store 在模块加载时注册 402（余额不足）全局引导——必须被 import 一次
import { useBilling } from "./stores/billing";
import { navigate, parsePath, useRouter } from "./router";
import { confirmDialog, toast } from "./ui/feedback";
import { IconClapper, IconLogout, IconPlus, IconSystem, IconTrash } from "./ui/icons";

/** 短剧工作台分区（18 号 §56：集号作为 episodes 分区之后的路径段） */
const DRAMA_SECTIONS = ["episodes", "script", "assets", "observe"] as const;
type DramaSectionKey = (typeof DRAMA_SECTIONS)[number];

/** 判定字符串是否为合法短剧分区 */
function isDramaSection(v: string): v is DramaSectionKey {
  return (DRAMA_SECTIONS as readonly string[]).includes(v);
}

// ---------- 首页 ----------

function HomePage() {
  const [projects, setProjects] = useState<ProjectInfo[]>([]);
  const [showCreate, setShowCreate] = useState(false);
  /** 关键字搜索（项目名 / 简介 / 剧本一句话简介；本地过滤，列表本来就整份加载） */
  const [query, setQuery] = useState("");
  const [gpu, setGpu] = useState<{
    device_name: string;
    inference_ready: boolean;
    notes: string[];
  } | null>(null);
  const user = useAuth((s) => s.user);
  const logout = useAuth((s) => s.logout);

  useEffect(() => {
    api.listProjects().then(setProjects).catch(() => {});
    api.gpuInfo().then(setGpu).catch(() => {});
  }, []);

  /** 删除项目：确认弹窗后调删除接口（级联删除项目数据），成功后从列表移除 */
  const remove = async (p: ProjectInfo) => {
    const ok = await confirmDialog({
      title: "删除项目",
      message: `确定删除项目「${p.name}」？项目内画布、任务、会话等数据将一并删除，且不可恢复。`,
      confirmText: "删除",
      danger: true,
    });
    if (!ok) return;
    try {
      await api.deleteProject(p.id);
      setProjects((prev) => prev.filter((x) => x.id !== p.id));
      toast.success(`已删除项目「${p.name}」`);
    } catch (e) {
      toast.error(`删除失败：${e instanceof Error ? e.message : String(e)}`);
    }
  };

  // 关键字过滤：项目名 / 简介 / 剧本一句话简介，大小写不敏感
  const q = query.trim().toLowerCase();
  const filtered = q
    ? projects.filter((p) =>
        [p.name, p.description, p.logline]
          .some((s) => (s || "").toLowerCase().includes(q)))
    : projects;

  return (
    <div className="home-page">
      {/* 顶部品牌渐变线（氛围点缀） */}
      <div className="home-accent" aria-hidden="true" />
      <header className="home-topbar">
        <div className="home-brand">
          <span className="home-logo" aria-hidden="true"><BrandMark /></span>
          <span>鲸影</span>
        </div>
        <div className="topbar-right">
          {/* GPU 状态弱化展示：小圆点 + 简短文案，详情入 tooltip */}
          {gpu && (
            <span
              className={`topbar-gpu ${gpu.inference_ready ? "ok" : "mock"}`}
              title={gpu.inference_ready
                ? `GPU 就绪：${gpu.device_name}`
                : `推理依赖未安装（mock 模式）：${gpu.notes[0] ?? ""}`}
            >
              {gpu.inference_ready ? "GPU 就绪" : "mock 模式"}
            </span>
          )}
          {/* 积分余额（33 号方案）：常显、点击进「我的积分」 */}
          <CreditBalance />
          <span className="topbar-user">欢迎您，{user?.username}</span>
          {user?.is_admin && (
            <button
              className="topbar-icon-btn"
              title="系统管理"
              aria-label="系统管理"
              onClick={() => navigate("#/admin/users")}
            >
              <IconSystem size={19} />
            </button>
          )}
          <button
            className="topbar-icon-btn"
            title="退出登录"
            aria-label="退出登录"
            onClick={() => {
              // 登出顺手清掉媒体签名缓存：签名与账号无关，留着会把上一个
              // 登录态签出的短时链接带进下一个会话
              clearSignedUrls();
              void logout();
            }}
          >
            <IconLogout size={19} />
          </button>
        </div>
      </header>
      <main className="home-main">
        <section className="home-hero">
          <h1>鲸影视频创作工作台</h1>
          <p className="sub">鲸纳万象 · 影叙人间</p>
        </section>
        {/* 列表工具栏：新建居左，右侧搜索 + 项目计数 */}
        <div className="home-toolbar">
          <button className="home-create" onClick={() => setShowCreate(true)}>
            <IconPlus size={16} />
            新建项目
          </button>
          <div className="home-toolbar-right">
            <input
              className="home-search"
              type="search"
              placeholder="搜索项目名称 / 简介"
              value={query}
              aria-label="搜索项目"
              onChange={(e) => setQuery(e.target.value)}
            />
            <span className="home-count">
              {q
                ? `匹配 ${filtered.length} / ${projects.length} 个项目`
                : `共 ${projects.length} 个项目`}
            </span>
          </div>
        </div>
        {filtered.length > 0 ? (
          <section className="home-cards">
            {filtered.map((p) => (
              <ProjectCard key={p.id} p={p} onOpen={() => navigate(`#/project/${p.id}`)}
                           onRemove={() => void remove(p)} />
            ))}
          </section>
        ) : projects.length > 0 ? (
          // 有项目但没匹配上：区分「真的没有项目」与「搜索没命中」
          <div className="home-empty">
            <IconClapper size={46} />
            <p>没有匹配「{query.trim()}」的项目，换个关键词试试</p>
          </div>
        ) : (
          <div className="home-empty">
            <IconClapper size={46} />
            <p>还没有项目，点击左上角「新建项目」开始创作</p>
          </div>
        )}
      </main>
      {showCreate && (
        <NewProjectDialog
          onClose={() => setShowCreate(false)}
          onCreated={(p) => {
            setShowCreate(false);
            navigate(`#/project/${p.id}`);
          }}
        />
      )}
    </div>
  );
}

/** 首页项目卡片：画风封面 + 类型/名称/一句话简介/标签 + 更新时间与删除。

描述优先取剧本解析产出的 logline（项目详情页 parse-logline 同源），
未解析回退项目描述，再回退占位文案。
 */
function ProjectCard({
  p,
  onOpen,
  onRemove,
}: {
  p: ProjectInfo;
  onOpen: () => void;
  onRemove: () => void;
}) {
  const desc = p.logline || p.description || "暂无简介";
  return (
    <div className={`project-card st-${p.style_preset}`} onClick={onOpen}>
      {/* 封面：画风配色的山峦分层景观（CSS 绘制，见 styles.css） */}
      <div className={`pc-cover cover-${p.style_preset}`}>
        <div className="pc-sky" />
        <div className="pc-mtn pc-mtn1" />
        <div className="pc-mtn pc-mtn2" />
        <div className="pc-haze" />
        <span className="pc-mode">{p.mode === "drama" ? "短剧&漫剧" : "自由画布"}</span>
        <span className="pc-country">{p.country}</span>
      </div>
      <div className="pc-body">
        <div className="pc-name" title={p.name}>{p.name}</div>
        <div className="pc-desc" title={desc}>{desc}</div>
        <div className="pc-meta">
          {p.mode === "drama" && (
            <span className={`pc-tag style-${p.style_preset}`}>
              {styleLabel(p.style_preset)}
            </span>
          )}
          <span className="pc-tag plain">{p.aspect_ratio}</span>
          <span className="pc-tag plain">{p.video_resolution}</span>
        </div>
        <div className="pc-foot">
          <span className="pc-time">更新于 {new Date(p.updated_at * 1000).toLocaleDateString()}</span>
          <button
            className="pc-delete"
            title="删除项目"
            aria-label="删除项目"
            onClick={(e) => {
              e.stopPropagation();
              onRemove();
            }}
          >
            <IconTrash size={15} />
          </button>
        </div>
      </div>
    </div>
  );
}

// ---------- 项目工作台（canvas / drama 两模式） ----------

function ProjectPage({ id, sub, seq }: { id: string; sub: string; seq?: string }) {
  const [project, setProject] = useState<ProjectInfo | null>(null);
  const [missing, setMissing] = useState(false);
  // 媒体签名到货后触发一次重渲染：`fileUrl()` 是同步取址，签名是异步申请的，
  // 所以首次渲染拿的是未签名地址、签名回来后必须重渲染一次才会换成签名地址
  // （方案 A 的取址层见 api/fileUrls.ts）。签名有效期 12 小时，续签同样走这里。
  const [, setSignEpoch] = useState(0);

  useEffect(() => {
    const onReady = () => setSignEpoch((n) => n + 1);
    window.addEventListener(SIGNED_URLS_READY, onReady);
    return () => window.removeEventListener(SIGNED_URLS_READY, onReady);
  }, []);

  // 载入项目元信息（刷新/直达按 URL 中的 ID 恢复）
  useEffect(() => {
    let alive = true;
    setProject(null);
    setMissing(false);
    api.getProject(id)
      .then((p) => {
        if (alive) setProject(p);
      })
      .catch(() => {
        if (alive) setMissing(true);
      });
    return () => {
      alive = false;
    };
  }, [id]);

  // 画布 store 同步：进入项目载入画布数据，离开（或切换项目）时清空
  useEffect(() => {
    void useCanvas.getState().setProject(id);
    return () => {
      void useCanvas.getState().setProject("");
      useDrama.getState().reset();
    };
  }, [id]);

  // 子路由归一：#/project/:id → 按模式补默认子页（replace 不留历史）。
  // 短剧默认落「分集工作台」（逐集流水线的主视图），非剧本/资产页
  useEffect(() => {
    if (!project) return;
    if (project.mode === "drama" && !isDramaSection(sub)) {
      navigate(`#/project/${id}/episodes`, true);
    } else if (project.mode === "canvas" && sub !== "canvas") {
      navigate(`#/project/${id}/canvas`, true);
    }
  }, [project, sub, id]);

  if (missing) {
    return (
      <div className="project-missing">
        <p>项目不存在或已被删除</p>
        <button className="topbar-btn" onClick={() => navigate("#/")}>返回首页</button>
      </div>
    );
  }
  if (!project) {
    return <div className="project-loading">加载中…</div>;
  }

  // 短剧模式：分区（episodes/script/assets/observe）+ 集号段（episodes/:seq）
  if (project.mode === "drama") {
    const section: DramaSectionKey = isDramaSection(sub) ? sub : "episodes";
    const parsedSeq = seq ? Number.parseInt(seq, 10) : NaN;
    return (
      <div className="app">
        <DramaView
          project={project}
          section={section}
          epSeq={Number.isFinite(parsedSeq) ? parsedSeq : null}
          onNavigate={(s, s2) => navigate(
            s2 ? `#/project/${id}/${s}/${s2}` : `#/project/${id}/${s}`)}
          onBack={() => navigate("#/")}
          onProjectUpdate={setProject}
        />
      </div>
    );
  }

  // 画布模式（子路由未归一时先渲染，归一 effect 随后 replace URL）
  return (
    <div className="app">
      <header className="app-header">
        <button className="back" onClick={() => navigate("#/")}>
          返回
        </button>
        <span>{project.name}</span>
        {/* 积分余额（33 号方案）：画布生成也花钱，这里常显 */}
        <CreditBalance />
      </header>
      <CanvasView />
    </div>
  );
}

// ---------- 路由分发 ----------

export function App() {
  const hash = useRouter((s) => s.hash);
  const segments = useMemo(() => parsePath(hash), [hash]);
  const user = useAuth((s) => s.user);
  const ready = useAuth((s) => s.ready);
  // 启动卡住的兜底：`bootstrap()` 拉 /api/auth/me，网络层已有超时；这里再给
  // 「一直没 ready」加一道时间闸——卡住时显示可重试的提示，而不是 `return null`
  // 那种一片空白的观感（2026-09-30 修白屏）。
  const [bootStuck, setBootStuck] = useState(false);

  // 启动恢复：有令牌则拉当前用户（只执行一次）
  useEffect(() => {
    void useAuth.getState().bootstrap();
    const t = window.setTimeout(() => setBootStuck(true), 8000);
    return () => window.clearTimeout(t);
  }, []);

  // ready 一到就撤掉兜底提示
  useEffect(() => {
    if (ready) setBootStuck(false);
  }, [ready]);

  // 登录后统一拉一次计费基础数据（余额 + 价目）：余额给顶栏，价目给
  // 花钱按钮的「消耗 N 积分」徽标（stores/billing 的缓存后续按需刷新）
  useEffect(() => {
    if (!user) return;
    void useBilling.getState().refreshAccount();
    void useBilling.getState().refreshPricing();
  }, [user]);

  // 未登录直达子页（如分享链接）：记住目标路径，登录后跳回
  useEffect(() => {
    if (ready && !user && parsePath(window.location.hash)[0] !== "login") {
      rememberRedirect();
    }
  }, [ready, user]);

  // 启动恢复中不渲染（避免未登录闪现首页）；卡住超过 8 秒给出可操作的提示
  if (!ready) {
    if (!bootStuck) return null;
    return (
      <div className="crash-screen">
        <div className="crash-card">
          <h2>正在连接后端…</h2>
          <p className="crash-hint">
            已经等了 8 秒还没收到响应。常见原因是后端没启动、端口被占用或网络不通。
            可以重试，或确认后端服务在运行。
          </p>
          <div className="crash-actions">
            <button className="mm-btn-primary" onClick={() => window.location.reload()}>
              重试
            </button>
          </div>
        </div>
      </div>
    );
  }

  // 未登录：一律登录页（登录成功后按记录的原路径跳回）
  if (!user) return <LoginPage />;

  // 仍是出厂默认口令：挡在强制改密页（换掉之前不放行任何工作台入口）
  if (user.must_change_password) return <ForcePasswordPage />;

  const [head, second, third, fourth] = segments;

  // 已登录访问 /login：回首页
  if (head === "login") return <HomePage />;
  // 我的积分（33 号方案）：余额 / 兑换码 / 明细
  if (head === "credits") return <CreditsPage />;
  // 系统管理（账号管理 + 系统设置，仅管理员；越权访问回首页）
  if (head === "admin") {
    return user.is_admin ? <SystemPage sub={second ?? ""} tab={third ?? ""} /> : <HomePage />;
  }
  // 项目工作台（/project/:id[/:sub[/:seq]]——短剧分区可带集号段）
  if (head === "project" && second) {
    return <ProjectPage id={second} sub={third ?? ""} seq={fourth} />;
  }
  // 默认：首页
  return <HomePage />;
}
