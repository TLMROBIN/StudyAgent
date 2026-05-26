export interface SubjectCount {
  subject: string
  count: number
}

export interface SubjectPieSlice extends SubjectCount {
  color: string
  percent: number
  label: string
  path: string
  labelX: number
  labelY: number
  lineStartX: number
  lineStartY: number
  lineEndX: number
  lineEndY: number
  textAnchor: 'start' | 'end'
}

export interface SubjectPieModel {
  width: number
  height: number
  centerX: number
  centerY: number
  radius: number
  total: number
  slices: SubjectPieSlice[]
}

interface PieModelOptions {
  width?: number
  height?: number
  centerX?: number
  centerY?: number
  radius?: number
  labelRadius?: number
  colors?: string[]
}

const DEFAULT_COLORS = ['#0f766e', '#db6b2c', '#2563eb', '#be123c', '#7c3aed', '#15803d', '#b45309', '#0891b2']

function polarToCartesian(centerX: number, centerY: number, radius: number, angleDegrees: number) {
  const angleRadians = (angleDegrees * Math.PI) / 180
  return {
    x: centerX + radius * Math.cos(angleRadians),
    y: centerY + radius * Math.sin(angleRadians),
  }
}

function formatPercent(value: number) {
  return `${(value * 100).toFixed(1)}%`
}

export function buildSubjectPieModel(items: SubjectCount[], options: PieModelOptions = {}): SubjectPieModel {
  const width = options.width ?? 520
  const height = options.height ?? 300
  const centerX = options.centerX ?? 150
  const centerY = options.centerY ?? 150
  const radius = options.radius ?? 86
  const labelRadius = options.labelRadius ?? 132
  const colors = options.colors?.length ? options.colors : DEFAULT_COLORS
  const total = items.reduce((sum, item) => sum + Math.max(item.count, 0), 0)
  let startAngle = -90

  return {
    width,
    height,
    centerX,
    centerY,
    radius,
    total,
    slices:
      total > 0
        ? items
            .filter((item) => item.count > 0)
            .map((item, index) => {
              const percent = item.count / total
              const endAngle = startAngle + percent * 360
              const sweepEndAngle = Math.min(endAngle, startAngle + 359.999)
              const start = polarToCartesian(centerX, centerY, radius, startAngle)
              const end = polarToCartesian(centerX, centerY, radius, sweepEndAngle)
              const midAngle = startAngle + (endAngle - startAngle) / 2
              const lineStart = polarToCartesian(centerX, centerY, radius + 4, midAngle)
              const lineEnd = polarToCartesian(centerX, centerY, labelRadius - 18, midAngle)
              const labelPoint = polarToCartesian(centerX, centerY, labelRadius, midAngle)
              const largeArcFlag = endAngle - startAngle > 180 ? 1 : 0
              const path = [
                `M ${centerX} ${centerY}`,
                `L ${start.x.toFixed(3)} ${start.y.toFixed(3)}`,
                `A ${radius} ${radius} 0 ${largeArcFlag} 1 ${end.x.toFixed(3)} ${end.y.toFixed(3)}`,
                'Z',
              ].join(' ')

              startAngle = endAngle

              return {
                ...item,
                color: colors[index % colors.length],
                percent,
                label: `${item.subject} ${formatPercent(percent)}（${item.count}次）`,
                path,
                labelX: labelPoint.x,
                labelY: Math.min(Math.max(labelPoint.y, 24), height - 24),
                lineStartX: lineStart.x,
                lineStartY: lineStart.y,
                lineEndX: lineEnd.x,
                lineEndY: lineEnd.y,
                textAnchor: labelPoint.x >= centerX ? 'start' : 'end',
              }
            })
        : [],
  }
}
