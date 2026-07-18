import { useEffect, useState } from 'react'

// Tiny bottom-left status readout. Call hud('message') from anywhere; it shows briefly then hides.
// Pass a longer ttl for messages worth reading (errors), 0 to clear immediately.
export const hud = (msg: string, ttl = 4000) =>
  window.dispatchEvent(new CustomEvent('hud', { detail: { msg, ttl } }))

export function Hud() {
  const [msg, setMsg] = useState('')
  useEffect(() => {
    let timer: number | undefined
    const onHud = (e: Event) => {
      const { msg, ttl } = (e as CustomEvent<{ msg: string; ttl: number }>).detail
      clearTimeout(timer)
      setMsg(msg)
      if (msg && ttl > 0) timer = window.setTimeout(() => setMsg(''), ttl)
    }
    window.addEventListener('hud', onHud as EventListener)
    return () => { window.removeEventListener('hud', onHud as EventListener); clearTimeout(timer) }
  }, [])
  return msg ? <div className="hud">{msg}</div> : null
}
