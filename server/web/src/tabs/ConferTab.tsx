import { useEffect, useMemo, useRef, useState } from 'react'
import * as api from '../api'
import { Unauthorized } from '../api'

/**
 * Admin view of Confer, a WhatsApp-style console. The folder/chat tree and participant roster
 * live on the left; the selected conversation and composer on the right. The owner posts as
 * "admin" (right-aligned), and a live WebSocket streams new messages in as they arrive.
 */
export function ConferTab({ onUnauthorized }: { onUnauthorized: () => void }) {
  const [tree, setTree] = useState<api.ConferTree>({ folders: [], chats: [], presence: [] })
  const [users, setUsers] = useState<api.ConferUser[]>([])
  const [chatId, setChatId] = useState<number | null>(null)
  const [messages, setMessages] = useState<api.ConferMessage[]>([])
  const [text, setText] = useState('')
  const [showUsers, setShowUsers] = useState(false)
  const [err, setErr] = useState('')
  const chatIdRef = useRef<number | null>(null)
  const scrollRef = useRef<HTMLDivElement>(null)
  const fileRef = useRef<HTMLInputElement>(null)

  const guard = async (fn: () => Promise<void>) => {
    try { await fn() } catch (e) { if (e instanceof Unauthorized) onUnauthorized(); else setErr(String((e as Error).message)) }
  }

  const loadTree = () => guard(async () => setTree(await api.conferTree()))
  const loadUsers = () => guard(async () => setUsers((await api.conferUsers()).users))
  const loadHistory = (id: number) => guard(async () => setMessages((await api.conferHistory(id)).messages))

  useEffect(() => { loadTree(); loadUsers() }, [])
  useEffect(() => { chatIdRef.current = chatId; if (chatId) loadHistory(chatId) }, [chatId])
  useEffect(() => { scrollRef.current?.scrollTo(0, scrollRef.current.scrollHeight) }, [messages])

  // Live channel: append messages for the open chat, refresh presence on any traffic.
  useEffect(() => {
    let ws: WebSocket | null = null
    let stop = false
    const connect = () => {
      if (stop) return
      ws = new WebSocket(api.conferWsUrl())
      ws.onmessage = (ev) => {
        try {
          const f = JSON.parse(ev.data)
          if (f.type === 'confer_msg' && f.chat_id === chatIdRef.current) {
            setMessages((m) => (m.some((x) => x.id === f.id) ? m : [...m, f as api.ConferMessage]))
          }
        } catch { /* ignore */ }
      }
      ws.onclose = () => { if (!stop) setTimeout(connect, 2000) }
    }
    connect()
    return () => { stop = true; ws?.close() }
  }, [])

  const chatsByFolder = useMemo(() => {
    const m = new Map<number | null, api.ConferChat[]>()
    for (const c of tree.chats) { const k = c.folder_id; if (!m.has(k)) m.set(k, []); m.get(k)!.push(c) }
    return m
  }, [tree])

  const activeChat = tree.chats.find((c) => c.id === chatId)

  const newFolder = () => guard(async () => {
    const name = prompt('Folder name'); if (!name) return
    setTree(await api.conferCreateFolder(name))
  })
  const newChat = (folderId: number | null) => guard(async () => {
    const name = prompt('Chat name'); if (!name) return
    setTree(await api.conferCreateChat(name, folderId))
  })
  const delChat = (id: number) => guard(async () => {
    if (!confirm('Delete this chat and its messages?')) return
    setTree(await api.conferDeleteChat(id)); if (chatId === id) { setChatId(null); setMessages([]) }
  })
  const delFolder = (id: number) => guard(async () => {
    if (!confirm('Delete this folder and everything in it?')) return
    setTree(await api.conferDeleteFolder(id))
  })

  const send = () => guard(async () => {
    if (!chatId || !text.trim()) return
    const r = await api.conferSendText(chatId, text.trim())
    if (!r.ok) { setErr((await r.json().catch(() => ({}))).error || 'Send failed'); return }
    setText('')
  })

  const sendImage = (file: File) => guard(async () => {
    if (!chatId) return
    const dataUrl: string = await new Promise((res, rej) => {
      const fr = new FileReader(); fr.onload = () => res(String(fr.result)); fr.onerror = rej; fr.readAsDataURL(file)
    })
    const b64 = dataUrl.includes('base64,') ? dataUrl.split('base64,')[1] : dataUrl
    const r = await api.conferSendImage(chatId, b64)
    if (!r.ok) setErr((await r.json().catch(() => ({}))).error || 'Image send failed')
  })

  const createUser = () => guard(async () => {
    const username = prompt('New Confer username'); if (!username) return
    const password = prompt('Password for ' + username); if (!password) return
    const display = prompt('Display name', username) || username
    const r = await api.conferCreateUser(username, password, display)
    if (!r.ok) { setErr((await r.json().catch(() => ({}))).error || 'Could not create user'); return }
    loadUsers()
  })

  return (
    <div className="confer-wrap" style={{ display: 'flex', gap: 12, height: '70vh' }}>
      {/* Left: tree + roster */}
      <div className="panel" style={{ width: 300, overflowY: 'auto', flexShrink: 0 }}>
        <div className="row" style={{ justifyContent: 'space-between', alignItems: 'center' }}>
          <strong>Chats</strong>
          <span>
            <button className="ghost mini" onClick={newFolder}>+folder</button>{' '}
            <button className="ghost mini" onClick={() => newChat(null)}>+chat</button>
          </span>
        </div>
        {tree.presence.length > 0 && (
          <div className="muted" style={{ fontSize: 11, margin: '6px 0' }}>
            <span className="dot on" /> in Confer now: {tree.presence.map((p) => p.display).join(', ')}
          </div>
        )}

        {tree.folders.map((f) => (
          <div key={f.id} style={{ marginTop: 8 }}>
            <div className="row" style={{ justifyContent: 'space-between' }}>
              <strong style={{ fontSize: 13 }}>📁 {f.name}</strong>
              <span>
                <button className="ghost mini" onClick={() => newChat(f.id)}>+</button>{' '}
                <button className="ghost mini" onClick={() => delFolder(f.id)}>✕</button>
              </span>
            </div>
            {(chatsByFolder.get(f.id) || []).map((c) => (
              <ChatRow key={c.id} c={c} active={c.id === chatId} onOpen={() => setChatId(c.id)} onDelete={() => delChat(c.id)} />
            ))}
          </div>
        ))}
        <div style={{ marginTop: 8 }}>
          {(chatsByFolder.get(null) || []).map((c) => (
            <ChatRow key={c.id} c={c} active={c.id === chatId} onOpen={() => setChatId(c.id)} onDelete={() => delChat(c.id)} />
          ))}
        </div>

        <hr style={{ margin: '12px 0', opacity: 0.2 }} />
        <button className="ghost mini" onClick={() => setShowUsers((v) => !v)}>
          {showUsers ? 'Hide' : 'Manage'} users ({users.length})
        </button>
        {showUsers && (
          <div style={{ marginTop: 8 }}>
            <button className="ghost mini" onClick={createUser}>+ new user</button>
            {users.map((u) => (
              <div key={u.id} className="row" style={{ justifyContent: 'space-between', marginTop: 6, fontSize: 12 }}>
                <span style={{ textDecoration: u.revoked ? 'line-through' : 'none' }}>{u.display_name} <span className="muted">@{u.username}</span></span>
                <button className="ghost mini" onClick={() => guard(async () => { await api.conferRevokeUser(u.id, !u.revoked); loadUsers() })}>
                  {u.revoked ? 'restore' : 'revoke'}
                </button>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Right: conversation */}
      <div className="panel" style={{ flex: 1, display: 'flex', flexDirection: 'column' }}>
        {activeChat ? (
          <>
            <div className="row" style={{ borderBottom: '1px solid rgba(128,128,128,.2)', paddingBottom: 6 }}>
              <strong># {activeChat.name}</strong>
            </div>
            <div ref={scrollRef} style={{ flex: 1, overflowY: 'auto', padding: '8px 4px' }}>
              {messages.map((m) => <Bubble key={m.id} m={m} />)}
            </div>
            <div className="row" style={{ gap: 6, alignItems: 'flex-end' }}>
              <textarea
                value={text} onChange={(e) => setText(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() } }}
                placeholder="Message as admin…" rows={2} style={{ flex: 1, resize: 'vertical' }} />
              <input ref={fileRef} type="file" accept="image/*" style={{ display: 'none' }}
                onChange={(e) => { const f = e.target.files?.[0]; if (f) sendImage(f); e.currentTarget.value = '' }} />
              <button className="ghost mini" onClick={() => fileRef.current?.click()}>img</button>
              <button onClick={send}>Send</button>
            </div>
          </>
        ) : (
          <div className="muted center" style={{ margin: 'auto' }}>Select a chat</div>
        )}
      </div>

      {err && <div className="err" style={{ position: 'fixed', bottom: 12, right: 12 }} onClick={() => setErr('')}>{err}</div>}
    </div>
  )
}

function ChatRow({ c, active, onOpen, onDelete }: { c: api.ConferChat; active: boolean; onOpen: () => void; onDelete: () => void }) {
  return (
    <div className="row" style={{ justifyContent: 'space-between', padding: '3px 6px', borderRadius: 6, cursor: 'pointer', background: active ? 'rgba(128,128,128,.18)' : 'transparent' }}>
      <span onClick={onOpen} style={{ flex: 1 }}># {c.name}</span>
      <button className="ghost mini" onClick={onDelete}>✕</button>
    </div>
  )
}

function Bubble({ m }: { m: api.ConferMessage }) {
  const mine = m.sender === 'admin'
  const body = m.kind === 'image'
    ? <img src={`data:image/png;base64,${m.body}`} alt="" style={{ maxWidth: 220, borderRadius: 8 }} />
    : m.body
  return (
    <div style={{ display: 'flex', justifyContent: mine ? 'flex-end' : 'flex-start', margin: '4px 0' }}>
      <div style={{ maxWidth: '75%', padding: '6px 10px', borderRadius: 10, background: mine ? '#2b6cb0' : 'rgba(128,128,128,.2)', color: mine ? '#fff' : 'inherit' }}>
        {!mine && <div style={{ fontSize: 11, opacity: 0.8, fontWeight: 600 }}>{m.sender_display}</div>}
        <div style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>{body}</div>
      </div>
    </div>
  )
}
