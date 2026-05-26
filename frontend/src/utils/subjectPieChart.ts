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
  minLabelGap?: number
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
  const width = options.width ?? 760
  const height = options.height ?? 340
  const centerX = options.centerX ?? 380
  const centerY = options.centerY ?? 170
  const radius = options.radius ?? 86
  const labelRadius = options.labelRadius ?? 132
  const minLabelGap = options.minLabelGap ?? 24
  const colors = options.colors?.length ? options.colors : DEFAULT_COLORS
  const total = items.reduce((sum, item) => sum + Math.max(item.count, 0), 0)
  let startAngle = -90
  const minLabelY = 24
  const maxLabelY = height - 24
  const labelColumnOffset = 168

  const slices =
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
              const labelPoint = polarToCartesian(centerX, centerY, labelRadius, midAngle)
              const textAnchor = labelPoint.x >= centerX ? 'start' : 'end'
              const labelX = textAnchor === 'start' ? centerX + labelColumnOffset : centerX - labelColumnOffset
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
                labelX,
                labelY: Math.min(Math.max(labelPoint.y, minLabelY), maxLabelY),
                lineStartX: lineStart.x,
                lineStartY: lineStart.y,
                lineEndX: textAnchor === 'start' ? labelX - 10 : labelX + 10,
                lineEndY: Math.min(Math.max(labelPoint.y, minLabelY), maxLabelY),
                textAnchor,
              }
            })
      : []

  for (const textAnchor of ['start', 'end'] as const) {
    const sideSlices = slices
      .filter((slice) => slice.textAnchor === textAnchor)
      .sort((first, second) => first.labelY - second.labelY)
    spreadLabelRows(sideSlices, minLabelY, maxLabelY, minLabelGap)
  }

  return {
    width,
    height,
    centerX,
    centerY,
    radius,
    total,
    slices,
  }
}

function spreadLabelRows(slices: SubjectPieSlice[], minY: number, maxY: number, gap: number) {
  if (!slices.length) {
    return
  }

  for (let index = 0; index < slices.length; index += 1) {
    const previousY = index === 0 ? minY - gap : slices[index - 1].labelY
    slices[index].labelY = Math.max(slices[index].labelY, previousY + gap)
  }

  const overflow = slices[slices.length - 1].labelY - maxY
  if (overflow > 0) {
    slices[slices.length - 1].labelY = maxY
    for (let index = slices.length - 2; index >= 0; index -= 1) {
      slices[index].labelY = Math.min(slices[index].labelY, slices[index + 1].labelY - gap)
    }
  }

  const underflow = minY - slices[0].labelY
  if (underflow > 0) {
    slices.forEach((slice) => {
      slice.labelY += underflow
    })
  }

  slices.forEach((slice) => {
    slice.lineEndY = slice.labelY - 4
  })
}
