// Branding mark for SVG charts: the site's globe logo followed by
// "The Metascience Observatory". Anchored to the top-left of the plot area by
// default (`x`/`y` locate the logo); pass `rightX` instead to right-anchor the
// mark with its text ending at that coordinate. Must be rendered inside an
// <svg> element (or a <g> within one), typically as the last child so it draws
// on top: <ChartWatermark /> or <ChartWatermark rightX={innerW - 8} />.
export function ChartWatermark({
  x = 8,
  y = 4,
  rightX,
  color = "currentColor",
}: {
  x?: number;
  y?: number;
  rightX?: number;
  color?: string;
}) {
  const label = "The Metascience Observatory";
  const textW = label.length * 4.9; // rough 10px-font width, only used when right-anchoring
  const logoX = rightX !== undefined ? rightX - textW - 23 : x;
  const textX = rightX !== undefined ? rightX : x + 17;
  return (
    <g opacity={0.8}>
      {/* globe logo (inline copy of public/assets/globe.svg), 12.8px tall */}
      <g
        transform={`translate(${logoX}, ${y}) scale(0.8)`}
        stroke={color}
        strokeWidth={0.9}
        strokeLinecap="round"
        fill="none"
      >
        <circle cx={8} cy={8} r={7.35} />
        <ellipse cx={8} cy={8} rx={3} ry={7.35} />
        <ellipse cx={8} cy={8} rx={7.35} ry={3} />
      </g>
      <text
        x={textX}
        y={y + 10}
        textAnchor={rightX !== undefined ? "end" : "start"}
        fill={color}
        style={{ fontSize: 10 }}
      >
        {label}
      </text>
    </g>
  );
}
