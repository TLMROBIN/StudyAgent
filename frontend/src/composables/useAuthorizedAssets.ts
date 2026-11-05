import { onBeforeUnmount, reactive } from 'vue'

import { api, resolveApiUrl, type AuthorizedAssetResource } from '../utils/api'

function assetKey(asset: AuthorizedAssetResource): string {
  const stableId = (
    ('asset_id' in asset && asset.asset_id)
    || ('attachment_id' in asset && asset.attachment_id)
    || asset.url
  )
  return `${stableId}::${asset.url}`
}

export function useAuthorizedAssets() {
  const assetUrls = reactive<Record<string, string>>({})
  const assetErrors = reactive<Record<string, boolean>>({})
  let disposed = false
  const pendingLoads = new Map<string, Promise<string>>()

  async function ensureAssetUrl(asset: AuthorizedAssetResource): Promise<string> {
    const key = assetKey(asset)
    if (assetUrls[key]) {
      return assetUrls[key]
    }

    const pending = pendingLoads.get(key)
    if (pending) {
      return pending
    }

    assetErrors[key] = false
    const request = api.get(resolveApiUrl(asset.url), { responseType: 'blob' })
      .then(({ data }) => {
        if (disposed) return ''
        const nextUrl = URL.createObjectURL(data)
        const previousUrl = assetUrls[key]
        if (previousUrl) {
          URL.revokeObjectURL(previousUrl)
        }
        assetUrls[key] = nextUrl
        return nextUrl
      })
      .catch((error) => {
        if (!disposed) assetErrors[key] = true
        throw error
      })
      .finally(() => {
        pendingLoads.delete(key)
      })

    pendingLoads.set(key, request)
    return request
  }

  function markAssetFailed(asset: AuthorizedAssetResource) {
    const key = assetKey(asset)
    if (assetUrls[key]) URL.revokeObjectURL(assetUrls[key])
    delete assetUrls[key]
    assetErrors[key] = true
  }

  function assetFailed(asset: AuthorizedAssetResource): boolean {
    return Boolean(assetErrors[assetKey(asset)])
  }

  function assetUrl(asset: AuthorizedAssetResource): string {
    return assetUrls[assetKey(asset)] || ''
  }

  async function preloadAssets(assets: AuthorizedAssetResource[]): Promise<void> {
    const uniqueAssets = assets.filter((asset, index, list) => {
      return list.findIndex((item) => assetKey(item) === assetKey(asset)) === index
    })
    await Promise.allSettled(uniqueAssets.map((asset) => ensureAssetUrl(asset)))
  }

  async function openAsset(asset: AuthorizedAssetResource): Promise<void> {
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
    disposed = true
    Object.values(assetUrls).forEach((url) => {
      if (url) {
        URL.revokeObjectURL(url)
      }
    })
  })

  return {
    assetUrl,
    assetFailed,
    markAssetFailed,
    ensureAssetUrl,
    openAsset,
    preloadAssets,
  }
}
