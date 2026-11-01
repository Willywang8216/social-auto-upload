// Shared by job, preparation and provider-status reads. Never retry a mutation.
export function isTransientError(error) {
  if (error?.code === 'ERR_INVALID_RESPONSE') return false
  const status = error?.response?.status >= 400
    ? error.response.status
    : Number(error?.response?.data?.code || error?.status)
  return !status || status >= 500 || [408, 425, 429].includes(status)
}

export function invalidResponse(message = '無效的狀態回應，已停止自動更新') {
  return Object.assign(new Error(message), { code: 'ERR_INVALID_RESPONSE' })
}

export function createPoller(fetchSnapshot, {
  onResult = () => {},
  onState = () => {},
  interval = 1500,
  maxInterval = 30000,
  maxErrors = 5,
} = {}) {
  const baseDelay = Math.max(1, interval)
  const delayLimit = Math.max(baseDelay, maxInterval)
  let active = false
  let timer = null
  let controller = null
  let inFlight = false
  let generation = 0
  let state = { status: 'idle', consecutiveErrors: 0, lastError: null, lastSuccessAt: null, nextPollAt: null }

  function update(patch) {
    state = { ...state, ...patch }
    onState({ ...state })
  }

  function finish(status) {
    active = false
    update({ status, nextPollAt: null })
  }

  function schedule(delay, status) {
    update({ status, nextPollAt: Date.now() + delay })
    timer = setTimeout(() => {
      timer = null
      tick()
    }, delay)
  }

  async function tick() {
    if (!active || inFlight) return
    inFlight = true
    const current = generation
    const requestController = new AbortController()
    controller = requestController
    const isCurrent = () => active && current === generation && !requestController.signal.aborted
    update({ status: 'polling', nextPollAt: null })
    try {
      const result = await fetchSnapshot(requestController.signal)
      if (!isCurrent()) return
      // Only a live generation may commit data; late responses after stop are ignored.
      const settled = onResult(result)
      if (!isCurrent()) return
      update({ consecutiveErrors: 0, lastError: null, lastSuccessAt: Date.now() })
      if (settled === 'completed' || settled === 'paused') finish(settled)
      else schedule(baseDelay, 'waiting')
    } catch (error) {
      if (!isCurrent()) return
      if (error?.code === 'ERR_CANCELED' || ['CanceledError', 'AbortError'].includes(error?.name)) {
        finish('stopped')
        return
      }
      const consecutiveErrors = state.consecutiveErrors + 1
      update({ consecutiveErrors, lastError: error?.response?.data?.msg || error?.message || '連線失敗' })
      if (!isTransientError(error) || consecutiveErrors >= maxErrors) finish('error')
      else schedule(Math.min(delayLimit, baseDelay * 2 ** consecutiveErrors), 'backoff')
    } finally {
      inFlight = false
      if (controller === requestController) controller = null
      // A stop/start during a slow read waits for that read to settle, even if
      // an adapter ignores AbortSignal. It cannot overlap or overwrite the new read.
      if (active && current !== generation) tick()
    }
  }

  return {
    start() {
      if (active) return
      active = true
      generation += 1
      update({ status: 'polling', consecutiveErrors: 0, lastError: null, nextPollAt: null })
      tick()
    },
    stop() {
      active = false
      generation += 1
      if (timer !== null) clearTimeout(timer)
      timer = null
      controller?.abort()
      update({ status: 'stopped', nextPollAt: null })
    },
    get active() { return active },
  }
}

export function pollingStatusText(state) {
  if (!state) return ''
  const next = state.nextPollAt ? new Date(state.nextPollAt).toLocaleTimeString() : ''
  switch (state.status) {
    case 'polling': return '正在更新狀態'
    case 'waiting': return '自動追蹤中'
    case 'backoff': return `連線暫停（${state.consecutiveErrors} 次），${next} 再確認：${state.lastError}`
    case 'error': return `自動更新已停止：${state.lastError}。可手動更新狀態，不會重新發佈。`
    case 'paused': return '需處理後手動更新狀態；不會自動重新發佈'
    case 'completed': return '狀態追蹤完成'
    case 'stopped': return '已停止狀態追蹤'
    default: return ''
  }
}
