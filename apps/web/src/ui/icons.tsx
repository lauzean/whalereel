/** SVG 线性图标集（24×24 网格，stroke 跟随 currentColor，顶部栏/侧栏菜单共用）。

设计约定：fill=none 纯描边，1.8 线宽圆角端点，与暗色工作台风格统一；
新增图标保持同规格（viewBox/线宽/圆角），颜色一律交由父级 color 控制。
 */

interface IconProps {
  /** 像素尺寸（宽高同值，缺省 18） */
  size?: number;
}

/** 图标通用描边属性（统一线宽与圆角） */
const STROKE = {
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 1.8,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
};

/** 系统管理：调节滑杆（设置/管理语义） */
export function IconSystem({ size = 18 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" {...STROKE} aria-hidden="true">
      <line x1="4" y1="7" x2="20" y2="7" />
      <circle cx="9.5" cy="7" r="2.4" />
      <line x1="4" y1="16.5" x2="20" y2="16.5" />
      <circle cx="15" cy="16.5" r="2.4" />
    </svg>
  );
}

/** 退出登录：门 + 向外箭头 */
export function IconLogout({ size = 18 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" {...STROKE} aria-hidden="true">
      <path d="M13.5 4.5H18a1.5 1.5 0 0 1 1.5 1.5v12a1.5 1.5 0 0 1-1.5 1.5h-4.5" />
      <path d="M9 12h10.5" />
      <path d="M16.5 8.5 20 12l-3.5 3.5" />
      <path d="M10.5 4.5H6A1.5 1.5 0 0 0 4.5 6v12A1.5 1.5 0 0 0 6 19.5h4.5" />
    </svg>
  );
}

/** 账号管理：双人像 */
export function IconUsers({ size = 18 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" {...STROKE} aria-hidden="true">
      <circle cx="9" cy="8.5" r="3.2" />
      <path d="M3.2 19.5c0-3.2 2.6-5.8 5.8-5.8s5.8 2.6 5.8 5.8" />
      <circle cx="16.8" cy="9.5" r="2.6" />
      <path d="M16.5 14.2c2.6.3 4.3 2.5 4.3 5.3" />
    </svg>
  );
}

/** 模型管理：钥匙（API Key 与端点） */
export function IconEndpoints({ size = 18 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" {...STROKE} aria-hidden="true">
      <circle cx="8" cy="15.5" r="4" />
      <path d="M11 12.5 20 3.5" />
      <path d="M17 6.5l2.5 2.5" />
      <path d="M14.5 9l2 2" />
    </svg>
  );
}

/** 本地模型：芯片 */
export function IconChip({ size = 18 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" {...STROKE} aria-hidden="true">
      <rect x="6.5" y="6.5" width="11" height="11" rx="2" />
      <rect x="10" y="10" width="4" height="4" rx="1" />
      <path d="M9 3.5v3M15 3.5v3M9 17.5v3M15 17.5v3M3.5 9h3M3.5 15h3M17.5 9h3M17.5 15h3" />
    </svg>
  );
}

/** 默认配置：开关（每类任务默认端点） */
export function IconDefaults({ size = 18 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" {...STROKE} aria-hidden="true">
      <rect x="3.5" y="8" width="17" height="8" rx="4" />
      <circle cx="15.5" cy="12" r="2.2" />
    </svg>
  );
}

/** 提示词：文档 + 行 */
export function IconPrompts({ size = 18 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" {...STROKE} aria-hidden="true">
      <path d="M7 3.5h7l4 4V19a1.5 1.5 0 0 1-1.5 1.5h-9A1.5 1.5 0 0 1 6 19V5A1.5 1.5 0 0 1 7.5 3.5Z" />
      <path d="M13.5 3.5V8H18" />
      <path d="M9 12.5h6M9 16h6" />
    </svg>
  );
}

/** 调用日志：折线脉冲（调用的波形 + 一根基线） */
export function IconCalls({ size = 18 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" {...STROKE} aria-hidden="true">
      <path d="M3.5 16.5h3l2.2-6 2.6 9 2.4-12 2 9 1.6-4.2h3.2" />
    </svg>
  );
}

/** 新建项目：加号圆角方块 */
export function IconPlus({ size = 18 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" {...STROKE} aria-hidden="true">
      <line x1="12" y1="5.5" x2="12" y2="18.5" />
      <line x1="5.5" y1="12" x2="18.5" y2="12" />
    </svg>
  );
}

/** 删除：垃圾桶 */
export function IconTrash({ size = 16 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" {...STROKE} aria-hidden="true">
      <path d="M4.5 7h15" />
      <path d="M9.5 7V5.2A1.7 1.7 0 0 1 11.2 3.5h1.6a1.7 1.7 0 0 1 1.7 1.7V7" />
      <path d="M6.5 7l.8 11.6a2 2 0 0 0 2 1.9h5.4a2 2 0 0 0 2-1.9L17.5 7" />
      <path d="M10.2 11v5.5M13.8 11v5.5" />
    </svg>
  );
}

/** 场记板（空状态/影视意象） */
export function IconClapper({ size = 18 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" {...STROKE} aria-hidden="true">
      <path d="M19.9 5.9 4.6 9.7a1.5 1.5 0 0 1-1.8-1.1l-.4-1.5a1.5 1.5 0 0 1 1.1-1.8l12.8-3.2a1.5 1.5 0 0 1 1.8 1.1l.4 1.5a1.5 1.5 0 0 1-1.1 1.8Z" />
      <path d="m6.8 5.4 2.2 3.2M11.9 4.1l2.2 3.2M17 2.9l2.2 3.2" />
      <path d="M3.5 12.5h17v6a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2v-6Z" />
      <path d="M3.5 12.5 20.5 12" />
    </svg>
  );
}

/** 技能包：三块积木（可组合的能力单元） */
export function IconBlocks({ size = 18 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" {...STROKE} aria-hidden="true">
      <rect x="8.5" y="3.5" width="7.5" height="7" rx="1.5" />
      <rect x="3.5" y="13.5" width="7.5" height="7" rx="1.5" />
      <rect x="13.5" y="13.5" width="7.5" height="7" rx="1.5" />
    </svg>
  );
}

/** 放大：对角双箭头 + 四角放大框（弹窗/全屏编辑的通用语义） */
export function IconExpand({ size = 18 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" {...STROKE} aria-hidden="true">
      <path d="M14.5 3.5h6v6" />
      <path d="M20.5 3.5 13 11" />
      <path d="M9.5 20.5h-6v-6" />
      <path d="M3.5 20.5 11 13" />
    </svg>
  );
}

/** 积分计费：叠放的硬币（积分/余额语义） */
export function IconCoins({ size = 18 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" {...STROKE} aria-hidden="true">
      <ellipse cx="12" cy="6.5" rx="7" ry="3" />
      <path d="M5 6.5v5c0 1.66 3.13 3 7 3s7-1.34 7-3v-5" />
      <path d="M5 11.5v5c0 1.66 3.13 3 7 3s7-1.34 7-3v-5" />
    </svg>
  );
}
