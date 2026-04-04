import { onBeforeUnmount, reactive } from 'vue'

import { api, resolveApiUrl, type KnowledgeAsset } from '../utils/api'

function assetKey(asset: KnowledgeAsset): string {
  return `${asset.asset_id}::${asset.url}`
}

export function useAuthorizedAssets() {
  const assetUrls = reactive<Record<string, string>>({})
  const pendingLoads = new Map<string, Promise<string>>()

  async function ensureAssetUrl(asset: KnowledgeAsset): Promise<string> {
    const key = assetKey(asset)
    if (assetUrls[key]) {
      return assetUrls[key]
    }

    const pending = pendingLoads.get(key)
    if (pending) {
      return pending
    }

    const request = api.get(resolveApiUrl(asset.url), { responseType: 'blob' })
      .then(({ data }) => {
        const nextUrl = URL.createObjectURL(data)
        const previousUrl = assetUrls[key]
        if (previousUrl) {
          URL.revokeObjectURL(previousUrl)
        }
        assetUrls[key] = nextUrl
        return nextUrl
      })
      .finally(() => {
        pendingLoads.delete(key)
      })

    pendingLoads.set(key, request)
    return request
  }

  function assetUrl(asset: KnowledgeAsset): string {
    return assetUrls[assetKey(asset)] || ''
  }

  async function preloadAssets(assets: KnowledgeAsset[]): Promise<void> {
    const uniqueAssets = assets.filter((asset, index, list) => {
      return list.findIndex((item) => assetKey(item) === assetKey(asset)) === index
    })
    await Promise.allSettled(uniqueAssets.map((asset) => ensureAssetUrl(asset)))
  }

  async function openAsset(asset: KnowledgeAsset): Promise<void> {
    const popup = window.open('', '_blank', 'noopener,noreferrer')
    try {
      const url = await ensureAssetUrl(asset)
      if (popup) {
        popup.location.href = url
        return
      }
      window.open(url, '_blank', 'noopener,noreferrer')
    } catch (error) {
      popup?.close()
      throw error
    }
  }

  onBeforeUnmount(() => {
    Object.values(assetUrls).forEach((url) => {
      if (url) {
        URL.revokeObjectURL(url)
      }
    })
  })

  return {
    assetUrl,
    openAsset,
    preloadAssets,
  }
}
