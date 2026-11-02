/** 品牌标记「鲸影」：一头鲸 + 身后胶片光束（品牌手册 §7 的 Logo 主形）。
 *
 * 图形本身是位图（`public/brand/logo.png`，源自 docs/brand/logo-whale-lightbeam-v5.png
 * 的裁切版：图形居中、白底方形，便于塞进方形图标位）。组件保留下来当**唯一出处**——
 * 首页顶栏 / 登录页 / 强制改密页三处都渲染它，换图形只改这一个文件。
 *
 * 底色由容器的白色方块提供（`.home-logo` / `.login-logo`），图片用 contain 铺满，
 * 于是鲸鱼完整可见、四周是白底——与原始设计稿的背景一致。
 */
export function BrandMark({ size }: { size?: number }) {
  return (
    <img
      src="/brand/logo.png"
      alt="鲸影"
      width={size ?? undefined}
      height={size ?? undefined}
      style={{ width: size ?? "100%", height: size ?? "100%", objectFit: "contain", display: "block" }}
    />
  );
}
