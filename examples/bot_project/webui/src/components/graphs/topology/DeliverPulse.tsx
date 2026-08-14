/**
 * DeliverPulse.tsx — deliver 脉冲签名元素(graph PRD §4.3,Rev 2)。
 *
 * 沿边路径从 source 移动到 target 的发光 teal 圆点 + 渐隐尾迹。
 *
 * - 圆点: --color-graph-deliver(brand-bright),r=4px,
 *   filter: drop-shadow(0 0 4px var(--color-graph-deliver-glow))
 * - 尾迹: 跟随圆点的 24px 路径段(--color-graph-deliver-trail,brand 40%),
 *   用 stroke-dasharray + stroke-dashoffset 动画
 * - 动画: 600ms(--dur-deliver),ease-out,rAF 驱动圆点沿 edgePathD(points) 移动
 * - 并发: 每个 DeliverPulse 实例独立,多条边同时 deliver 时并行不阻塞
 * - 生命周期: 挂载即播放,600ms 后调用 onComplete(外部移除该脉冲)
 * - 降级(prefers-reduced-motion: reduce): 不显示移动脉冲,
 *   改为边短暂高亮(stroke-graph-edge-active,220ms)
 *
 * 路径点计算的数学(pathLength / interpolatePathPoint / trailDashOffset)
 * 抽成纯函数,可脱离 DOM 单测。
 */
import { useEffect, useRef, type FC } from "react";
import { edgePathD } from "./GraphEdge";
import type { LayoutPoint } from "./layout";

// ── 常量(§4.3 / §8.1) ──────────────────────────────────────────

/** 圆点半径(§4.3: r=4px)。 */
export const DELIVER_PULSE_RADIUS = 4;
/** 尾迹长度(§4.3: 24px)。 */
export const DELIVER_TRAIL_LENGTH = 24;
/** 正常动画时长(§8.1: --dur-deliver = 600ms)。 */
export const DELIVER_DURATION_MS = 600;
/** 降级模式下边高亮时长(§8.2: --dur = 220ms)。 */
export const DELIVER_FALLBACK_MS = 220;

// ── 纯函数(可单测) ────────────────────────────────────────────

/**
 * 计算折线总长度(各段长度之和)。
 * points 为空或单点时返回 0。
 */
export function pathLength(points: LayoutPoint[]): number {
  if (points.length < 2) return 0;
  let total = 0;
  for (let i = 1; i < points.length; i++) {
    const a = points[i];
    const b = points[i - 1];
    if (!a || !b) break;
    total += Math.hypot(a.x - b.x, a.y - b.y);
  }
  return total;
}

/** ease-out 缓动: t → 1-(1-t)²。 */
export function easeOut(t: number): number {
  return 1 - (1 - t) * (1 - t);
}

/**
 * 沿折线插值,返回进度 t(0=起点, 1=终点)处的坐标。
 * t 被 clamp 到 [0, 1];空数组返回原点。
 */
export function interpolatePathPoint(
  points: LayoutPoint[],
  t: number,
): LayoutPoint {
  if (points.length === 0) return { x: 0, y: 0 };
  const first = points[0];
  if (!first) return { x: 0, y: 0 };
  if (points.length === 1) return { x: first.x, y: first.y };

  const clampedT = Math.max(0, Math.min(1, t));
  const total = pathLength(points);
  if (total === 0) return { x: first.x, y: first.y };

  const targetDist = clampedT * total;
  let acc = 0;
  for (let i = 1; i < points.length; i++) {
    const curr = points[i];
    const prev = points[i - 1];
    if (!curr || !prev) break;
    const dx = curr.x - prev.x;
    const dy = curr.y - prev.y;
    const segLen = Math.hypot(dx, dy);
    if (acc + segLen >= targetDist) {
      const segT = segLen > 0 ? (targetDist - acc) / segLen : 0;
      return {
        x: prev.x + dx * segT,
        y: prev.y + dy * segT,
      };
    }
    acc += segLen;
  }
  // 浮点精度兜底
  const last = points[points.length - 1];
  return last
    ? { x: last.x, y: last.y }
    : { x: first.x, y: first.y };
}

/**
 * 计算尾迹的 stroke-dashoffset 值。
 *
 * 尾迹是 trailLength 长度的路径段,前端对齐圆点位置(圆点在尾迹头部)。
 * dasharray = "trailLength [totalLength]" → 单个 trailLength dash。
 * dashoffset = trailLength - dotPos → dash 位于 [dotPos - trailLength, dotPos]。
 *
 * @param totalLength 路径总长度
 * @param trailLength 尾迹长度
 * @param t 动画进度 [0, 1]
 */
export function trailDashOffset(
  totalLength: number,
  trailLength: number,
  t: number,
): number {
  const clampedT = Math.max(0, Math.min(1, t));
  const dotPos = clampedT * totalLength;
  return trailLength - dotPos;
}

// ── Reduced-motion 检测 ────────────────────────────────────────

/**
 * 检测用户是否偏好减少动效。
 * happy-dom 可能不实现 matchMedia,此时返回 false(正常模式)。
 */
export function prefersReducedMotion(): boolean {
  return (
    typeof window !== "undefined" &&
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );
}

// ── 组件 ───────────────────────────────────────────────────────

export interface DeliverPulseProps {
  /** 边路径点(含首尾,顺序 source → target)。 */
  points: LayoutPoint[];
  /** 600ms 后调用(正常模式)或 220ms 后(降级模式),外部移除该脉冲。 */
  onComplete?: () => void;
}

export const DeliverPulse: FC<DeliverPulseProps> = ({ points, onComplete }) => {
  const dotRef = useRef<SVGCircleElement | null>(null);
  const trailRef = useRef<SVGPathElement | null>(null);
  const totalLen = pathLength(points);
  const d = edgePathD(points);
  const reduced = prefersReducedMotion();

  // onComplete 用 ref 持有,避免回调变更重启动画。
  const cbRef = useRef(onComplete);
  useEffect(() => {
    cbRef.current = onComplete;
  });

  useEffect(() => {
    // 退化输入:无路径,立即完成。
    if (points.length < 2 || totalLen === 0) {
      cbRef.current?.();
      return;
    }

    // 降级模式:不播放移动脉冲,220ms 后完成。
    if (reduced) {
      const timer = setTimeout(() => cbRef.current?.(), DELIVER_FALLBACK_MS);
      return () => clearTimeout(timer);
    }

    // 正常模式:rAF 驱动圆点沿路径移动 + 尾迹 dashoffset。
    // 用 rAF 回调的 timestamp(now 参数)而非 performance.now() 计算 elapsed
    // — fake-timer 环境下 rAF timestamp 与时钟同步,测试更可靠。
    let rafId = 0;
    let started = false;
    let start = 0;

    const tick = (now: number) => {
      if (!started) {
        start = now;
        started = true;
      }
      const elapsed = now - start;
      const rawT = Math.min(1, elapsed / DELIVER_DURATION_MS);
      const t = easeOut(rawT);

      const pt = interpolatePathPoint(points, t);
      if (dotRef.current) {
        dotRef.current.setAttribute("cx", String(pt.x));
        dotRef.current.setAttribute("cy", String(pt.y));
      }

      if (trailRef.current) {
        const offset = trailDashOffset(totalLen, DELIVER_TRAIL_LENGTH, t);
        trailRef.current.setAttribute("stroke-dashoffset", String(offset));
      }

      if (rawT < 1) {
        rafId = requestAnimationFrame(tick);
      } else {
        cbRef.current?.();
      }
    };

    rafId = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(rafId);
  }, [points, totalLen, reduced]);

  // 降级模式:渲染静态高亮边(§8.2)。
  if (reduced) {
    return (
      <path
        d={d}
        fill="none"
        strokeWidth={1.5}
        className="stroke-graph-edge-active"
        data-testid="deliver-pulse-fallback"
        pointerEvents="none"
      />
    );
  }

  if (points.length < 2) return null;

  // 正常模式:圆点 + 尾迹。
  const startPos = points[0]!;
  return (
    <g data-testid="deliver-pulse" pointerEvents="none">
      {/* 尾迹:24px 渐隐路径段(brand 40% → 0% via dasharray) */}
      <path
        ref={trailRef}
        d={d}
        fill="none"
        strokeWidth={3}
        stroke="var(--color-graph-deliver-trail)"
        strokeLinecap="round"
        strokeDasharray={`${DELIVER_TRAIL_LENGTH} ${totalLen}`}
        strokeDashoffset={DELIVER_TRAIL_LENGTH}
      />
      {/* 圆点:brand-bright, r=4, 12% glow */}
      <circle
        ref={dotRef}
        r={DELIVER_PULSE_RADIUS}
        fill="var(--color-graph-deliver)"
        cx={startPos.x}
        cy={startPos.y}
        style={{
          filter: "drop-shadow(0 0 4px var(--color-graph-deliver-glow))",
        }}
      />
    </g>
  );
};
