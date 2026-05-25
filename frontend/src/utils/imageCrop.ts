export interface CropRect {
  x: number
  y: number
  width: number
  height: number
}

export interface ImageSize {
  width: number
  height: number
}

export function clampCropRectToImage(rect: CropRect, image: ImageSize): CropRect {
  const x = Math.max(0, Math.min(rect.x, Math.max(1, image.width) - 1))
  const y = Math.max(0, Math.min(rect.y, Math.max(1, image.height) - 1))
  const maxWidth = Math.max(1, image.width - x)
  const maxHeight = Math.max(1, image.height - y)
  return {
    x: Math.round(x),
    y: Math.round(y),
    width: Math.round(Math.max(1, Math.min(rect.width, maxWidth))),
    height: Math.round(Math.max(1, Math.min(rect.height, maxHeight))),
  }
}

export function previewRectToNaturalRect(rect: CropRect, preview: ImageSize, natural: ImageSize): CropRect {
  const scaleX = natural.width / Math.max(1, preview.width)
  const scaleY = natural.height / Math.max(1, preview.height)
  return clampCropRectToImage(
    {
      x: rect.x * scaleX,
      y: rect.y * scaleY,
      width: rect.width * scaleX,
      height: rect.height * scaleY,
    },
    natural,
  )
}

export async function createCroppedImageFile(
  source: File,
  image: HTMLImageElement,
  cropRect: CropRect,
): Promise<File> {
  const canvas = document.createElement('canvas')
  canvas.width = cropRect.width
  canvas.height = cropRect.height

  const context = canvas.getContext('2d')
  if (!context) {
    throw new Error('当前浏览器不支持图片裁剪')
  }

  context.drawImage(
    image,
    cropRect.x,
    cropRect.y,
    cropRect.width,
    cropRect.height,
    0,
    0,
    cropRect.width,
    cropRect.height,
  )

  const mimeType = source.type === 'image/png' ? 'image/png' : 'image/jpeg'
  const blob = await new Promise<Blob>((resolve, reject) => {
    canvas.toBlob((result) => {
      if (result) {
        resolve(result)
      } else {
        reject(new Error('图片裁剪失败，请重试'))
      }
    }, mimeType, 0.92)
  })

  return new File([blob], croppedImageName(source.name, mimeType), {
    type: mimeType,
    lastModified: Date.now(),
  })
}

function croppedImageName(filename: string, mimeType: string): string {
  const extension = mimeType === 'image/png' ? 'png' : 'jpg'
  const basename = filename.replace(/\.[^.]+$/, '') || 'question-image'
  return `${basename}-crop.${extension}`
}
