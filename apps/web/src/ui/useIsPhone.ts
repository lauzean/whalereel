/** 手机档判定（≤768px）。

用途很窄：同一份 DOM 在手机/桌面走**不同呈现**、而**个别状态必须由 JS 决定**时
（例：画布对话面板在手机上是全屏浮层，首屏默认展开会盖住画布），用它在挂载时
做一次性分流。能用 CSS 媒体查询表达的样式**一律不要**走这个 hook——面板显隐、
尺寸、排列都应由 styles-mobile.css 负责，避免出现「JS 判断的宽度」与
「CSS 媒体查询的宽度」两套真相。
 */

import { useEffect, useState } from "react";

/** 手机档最大宽度（px）：与 styles-mobile.css 的 @media (max-width: 768px) 对应 */
const PHONE_MAX_WIDTH = 768;

/** 当前是否手机档（随视口变化实时更新） */
export function useIsPhone(): boolean {
  const [isPhone, setIsPhone] = useState(
    () => window.matchMedia(`(max-width: ${PHONE_MAX_WIDTH}px)`).matches,
  );

  useEffect(() => {
    const mq = window.matchMedia(`(max-width: ${PHONE_MAX_WIDTH}px)`);
    const onChange = (e: MediaQueryListEvent) => setIsPhone(e.matches);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);

  return isPhone;
}
