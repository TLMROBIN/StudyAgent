import assert from 'node:assert/strict'

import { shouldShowOidcLogin } from '../src/utils/oidcConfig.ts'

assert.equal(shouldShowOidcLogin({ enabled: true }), true)
assert.equal(shouldShowOidcLogin({ enabled: false }), false)
assert.equal(shouldShowOidcLogin(null), false)
assert.equal(shouldShowOidcLogin(undefined), false)
