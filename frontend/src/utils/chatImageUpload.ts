export const MAX_CHAT_IMAGE_LONG_EDGE = 2000
export const CHAT_IMAGE_JPEG_QUALITY = 0.85
export const MAX_CHAT_IMAGE_BYTES = 8 * 1024 * 1024
export const CHAT_IMAGE_TOO_LARGE_MESSAGE = '图片过大，请重拍或截取题目区域'
export const CHAT_IMAGE_QUALITY_WARNING_MESSAGE = '照片可能模糊/过暗，建议重拍'
export const HEIC_EXPORT_MESSAGE = '请在相册中将照片导出为 JPG 后上传'

const BLUR_LAPLACIAN_VARIANCE_THRESHOLD = 80
const DARK_BRIGHTNESS_THRESHOLD = 55
const QUALITY_ANALYSIS_LONG_EDGE = 320

export type ChatImageQualityIssue = 'blur' | 'dark'

export interface ChatImageQualityReport {
  averageBrightness: number
  laplacianVariance: number
  issues: ChatImageQualityIssue[]
}

export interface PreparedChatImageUpload {
  file: File
  qualityWarnings: string[]
  qualityReport: ChatImageQualityReport | null
}

export function isSupportedChatImageFile(file: File): boolean {
  return file.type.startsWith('image/') || /\.(png|jpe?g|webp|gif|heic|heif)$/i.test(file.name)
}

export async function prepareChatImageUpload(file: File): Promise<PreparedChatImageUpload> {
  if (!isSupportedChatImageFile(file)) {
    throw new Error('只支持上传 1 张图片')
  }

  const heic = isLikelyHeic(file)
  let image: HTMLImageElement
  try {
    image = await loadImage(file)
  } catch (error) {
    if (heic) {
      throw new Error(HEIC_EXPORT_MESSAGE)
    }
    return ensureSizeLimit({
      file,
      qualityWarnings: [],
      qualityReport: null,
    })
  }

  try {
    const canvas = drawImageToCanvas(image, resizedDimensions(image.naturalWidth, image.naturalHeight))
    const qualityReport = analyzeCanvasQuality(canvas)
    const qualityWarnings = qualityReport.issues.length ? [CHAT_IMAGE_QUALITY_WARNING_MESSAGE] : []
    const blob = await canvasToJpegBlob(canvas)
    const preparedFile = new File([blob], jpegFileName(file.name), {
      type: 'image/jpeg',
      lastModified: file.lastModified || Date.now(),
    })
    return ensureSizeLimit({
      file: preparedFile,
      qualityWarnings,
      qualityReport,
    })
  } catch (error) {
    if (heic) {
      throw new Error(HEIC_EXPORT_MESSAGE)
    }
    return ensureSizeLimit({
      file,
      qualityWarnings: [],
      qualityReport: null,
    })
  }
}

function ensureSizeLimit(result: PreparedChatImageUpload): PreparedChatImageUpload {
  if (result.file.size > MAX_CHAT_IMAGE_BYTES) {
    throw new Error(CHAT_IMAGE_TOO_LARGE_MESSAGE)
  }
  return result
}

function isLikelyHeic(file: File): boolean {
  return /image\/hei[cf]/i.test(file.type) || /\.(heic|heif)$/i.test(file.name)
}

function loadImage(file: File): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file)
    const image = new Image()
    image.onload = () => {
      URL.revokeObjectURL(url)
      resolve(image)
    }
    image.onerror = () => {
      URL.revokeObjectURL(url)
      reject(new Error('image decode failed'))
    }
    image.src = url
  })
}

function resizedDimensions(width: number, height: number): { width: number; height: number } {
  const longEdge = Math.max(width, height)
  if (!longEdge || longEdge <= MAX_CHAT_IMAGE_LONG_EDGE) {
    return {
      width: Math.max(1, Math.round(width)),
      height: Math.max(1, Math.round(height)),
    }
  }
  const scale = MAX_CHAT_IMAGE_LONG_EDGE / longEdge
  return {
    width: Math.max(1, Math.round(width * scale)),
    height: Math.max(1, Math.round(height * scale)),
  }
}

function drawImageToCanvas(image: HTMLImageElement, size: { width: number; height: number }): HTMLCanvasElement {
  const canvas = document.createElement('canvas')
  canvas.width = size.width
  canvas.height = size.height
  const context = canvas.getContext('2d')
  if (!context) {
    throw new Error('canvas unavailable')
  }
  context.drawImage(image, 0, 0, size.width, size.height)
  return canvas
}

function canvasToJpegBlob(canvas: HTMLCanvasElement): Promise<Blob> {
  return new Promise((resolve, reject) => {
    canvas.toBlob((blob) => {
      if (blob) {
        resolve(blob)
      } else {
        reject(new Error('image compression failed'))
      }
    }, 'image/jpeg', CHAT_IMAGE_JPEG_QUALITY)
  })
}

function analyzeCanvasQuality(sourceCanvas: HTMLCanvasElement): ChatImageQualityReport {
  const canvas = analysisCanvas(sourceCanvas)
  const context = canvas.getContext('2d')
  if (!context) {
    return { averageBrightness: 255, laplacianVariance: 999, issues: [] }
  }
  const { width, height } = canvas
  const pixels = context.getImageData(0, 0, width, height).data
  const grayscale = new Float64Array(width * height)
  let brightnessTotal = 0

  for (let index = 0; index < grayscale.length; index += 1) {
    const pixelIndex = index * 4
    const value = pixels[pixelIndex] * 0.299 + pixels[pixelIndex + 1] * 0.587 + pixels[pixelIndex + 2] * 0.114
    grayscale[index] = value
    brightnessTotal += value
  }

  const averageBrightness = brightnessTotal / Math.max(1, grayscale.length)
  const laplacianVariance = calculateLaplacianVariance(grayscale, width, height)
  const issues: ChatImageQualityIssue[] = []
  if (laplacianVariance < BLUR_LAPLACIAN_VARIANCE_THRESHOLD) {
    issues.push('blur')
  }
  if (averageBrightness < DARK_BRIGHTNESS_THRESHOLD) {
    issues.push('dark')
  }
  return { averageBrightness, laplacianVariance, issues }
}

function analysisCanvas(sourceCanvas: HTMLCanvasElement): HTMLCanvasElement {
  const size = resizedAnalysisDimensions(sourceCanvas.width, sourceCanvas.height)
  if (size.width === sourceCanvas.width && size.height === sourceCanvas.height) {
    return sourceCanvas
  }
  const canvas = document.createElement('canvas')
  canvas.width = size.width
  canvas.height = size.height
  const context = canvas.getContext('2d')
  if (!context) {
    return sourceCanvas
  }
  context.drawImage(sourceCanvas, 0, 0, size.width, size.height)
  return canvas
}

function resizedAnalysisDimensions(width: number, height: number): { width: number; height: number } {
  const longEdge = Math.max(width, height)
  if (!longEdge || longEdge <= QUALITY_ANALYSIS_LONG_EDGE) {
    return { width, height }
  }
  const scale = QUALITY_ANALYSIS_LONG_EDGE / longEdge
  return {
    width: Math.max(1, Math.round(width * scale)),
    height: Math.max(1, Math.round(height * scale)),
  }
}

function calculateLaplacianVariance(grayscale: Float64Array, width: number, height: number): number {
  if (width < 3 || height < 3) {
    return 999
  }
  let total = 0
  let totalSquared = 0
  let count = 0
  for (let y = 1; y < height - 1; y += 1) {
    for (let x = 1; x < width - 1; x += 1) {
      const index = y * width + x
      const laplacian = (
        grayscale[index - width]
        + grayscale[index - 1]
        - grayscale[index] * 4
        + grayscale[index + 1]
        + grayscale[index + width]
      )
      total += laplacian
      totalSquared += laplacian * laplacian
      count += 1
    }
  }
  const mean = total / Math.max(1, count)
  return totalSquared / Math.max(1, count) - mean * mean
}

function jpegFileName(filename: string): string {
  const basename = filename.replace(/\.[^.]+$/, '') || 'question-image'
  return `${basename}.jpg`
}
