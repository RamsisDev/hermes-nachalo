// Nachalo for Hermes Desktop: pair by six spoken words, compare safety words, send drafts.
// Plain ESM loaded uncompiled: no JSX, SDK components only, theme variables only. The renderer
// never holds keys or topics; the six words live in component state until the pairing ends.
import {
  Badge,
  Button,
  Codicon,
  EmptyState,
  Input,
  ROUTES_AREA,
  SIDEBAR_NAV_AREA,
  STATUSBAR_AREAS,
  Tip,
  host,
  relativeTime,
} from '@hermes/plugin-sdk'
import { useCallback, useEffect, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'nachalo'
const TABS = ['pair', 'join', 'peers', 'inbox', 'drafts']
const TAB_LABELS = { pair: 'Pair', join: 'Join', peers: 'Peers', inbox: 'Inbox', drafts: 'Drafts' }

let rest = null
const listeners = new Set()

function emit() {
  for (const fn of listeners) fn()
}

function useRefresh() {
  const [tick, setTick] = useState(0)
  useEffect(() => {
    const fn = () => setTick(t => t + 1)
    listeners.add(fn)
    return () => listeners.delete(fn)
  }, [])
  return tick
}

function errorText(err) {
  if (!err) return ''
  return err.detail || err.message || String(err)
}

const text = (className, children) => jsx('div', { className, children })
const muted = children => text('text-sm text-(--ui-text-tertiary)', children)

// Shared look for every screen. Theme variables only, so every theme (light and dark) works.
const ui = {
  page: 'mx-auto flex w-full max-w-3xl flex-col gap-6 px-8 py-8',
  card: 'flex flex-col gap-4 rounded-xl border border-(--ui-stroke-secondary) bg-(--ui-bg-card) p-6',
  heading: 'text-base font-semibold text-(--ui-text-primary)',
  body: 'text-sm leading-6 text-(--ui-text-secondary)',
  hint: 'text-xs leading-5 text-(--ui-text-tertiary)',
  actions: 'flex flex-wrap items-center gap-2',
  good: 'text-(--ui-green)',
  bad: 'text-(--ui-red)'
}

function Card({ title, subtitle, children }) {
  return jsxs('section', {
    className: ui.card,
    // Disk plugins are not scanned by the app's Tailwind build, so a background class core never
    // uses would not exist; the fill goes inline.
    style: { background: 'var(--ui-bg-card)' },
    children: [
      title || subtitle
        ? jsxs('div', {
            className: 'flex flex-col gap-1',
            children: [title ? text(ui.heading, title) : null, subtitle ? text(ui.body, subtitle) : null]
          })
        : null,
      children
    ]
  })
}

// Pairing flow (Pair and Join tabs). Disk plugins are not scanned by Tailwind, so only classes the
// app already ships render; tints and anything else go through inline theme variables.
const PAIR_TTL_SECONDS = 600
const PAIR_ENDED = ['paired', 'mismatch', 'failed']
const PAIR_STEPS = ['Share words', 'Compare safety words', 'Paired']
const PAIR_JOIN_STEPS = ['Enter words', 'Read safety words', 'Paired']

const pairTint = (variable, percent) => `color-mix(in srgb, var(${variable}) ${percent}%, transparent)`
const pairNeutral = percent => pairTint('--ui-text-primary', percent)
const pairWordCount = value => value.trim().split(/\s+/).filter(Boolean).length
const pairOnEnter = (enabled, fn) => e => {
  if (e.key === 'Enter' && !e.nativeEvent.isComposing && enabled) fn()
}

function PairStepper({ steps, current }) {
  return jsx('ol', {
    className: 'flex items-center gap-3',
    'aria-label': 'Pairing steps',
    children: steps.map((label, i) => {
      const done = i < current
      const active = i === current
      const last = i === steps.length - 1
      const dot = done
        ? { background: pairTint('--ui-green', 16), color: 'var(--ui-green)' }
        : active
          ? undefined
          : { boxShadow: 'inset 0 0 0 1px var(--ui-stroke-secondary)', color: 'var(--ui-text-tertiary)' }
      return jsxs('li', {
        className: `flex min-w-0 items-center gap-2 ${last ? '' : 'flex-1'}`,
        'aria-current': active ? 'step' : undefined,
        children: [
          jsx('span', {
            className: `grid size-6 shrink-0 place-items-center rounded-full text-xs font-semibold tabular-nums ${
              active ? 'bg-primary text-primary-foreground' : ''
            }`,
            style: dot,
            children: done ? jsx(Codicon, { name: 'check', size: 12 }) : i + 1
          }),
          jsx('span', {
            className: `truncate text-xs ${
              active ? 'font-medium text-(--ui-text-primary)' : 'text-(--ui-text-tertiary)'
            }`,
            children: label
          }),
          last
            ? null
            : jsx('span', {
                className: 'h-px min-w-4 flex-1',
                style: { background: done ? pairTint('--ui-green', 45) : 'var(--ui-stroke-tertiary)' }
              })
        ]
      }, label)
    })
  })
}

function PairLayout({ steps, current, children }) {
  return jsxs('div', {
    className: 'flex w-full flex-col gap-5 pt-1',
    children: [steps ? jsx(PairStepper, { steps, current }) : null, jsx(Card, { children })]
  })
}

function PairBody({ children }) {
  return jsx('div', { className: 'flex flex-col gap-5', children })
}

function PairHeader({ title, subtitle }) {
  return jsxs('div', {
    className: 'flex flex-col gap-1',
    children: [text(ui.heading, title), subtitle ? text(ui.body, subtitle) : null]
  })
}

function PairStatus({ children }) {
  return jsxs('div', {
    className: 'flex items-center gap-2 text-sm text-(--ui-text-secondary)',
    role: 'status',
    children: [jsx(Codicon, { name: 'loading', spinning: true, className: 'text-(--ui-accent)' }), children]
  })
}

function PairNote({ children }) {
  if (!children) return null
  return jsxs('div', {
    role: 'alert',
    className: 'flex items-start gap-2 rounded-lg px-3 py-2.5 text-sm leading-5 text-(--ui-text-primary)',
    style: { background: pairTint('--ui-red', 9), boxShadow: `inset 0 0 0 1px ${pairTint('--ui-red', 28)}` },
    children: [
      jsx(Codicon, { name: 'warning', className: `mt-0.5 shrink-0 ${ui.bad}` }),
      jsx('span', { children })
    ]
  })
}

function PairWordCounter({ typed, want }) {
  const n = pairWordCount(typed)
  const color = n === want ? 'var(--ui-green)' : n > want ? 'var(--ui-yellow)' : 'var(--ui-text-tertiary)'
  return jsxs('span', {
    className: 'inline-flex items-center gap-1 text-xs tabular-nums',
    style: { color },
    'aria-live': 'polite',
    children: [n === want ? jsx(Codicon, { name: 'check', size: 12 }) : null, `${n} of ${want} words`]
  })
}

function PairField({ label, typed, want, hint, input }) {
  return jsxs('label', {
    className: 'flex flex-col gap-2',
    children: [
      jsxs('div', {
        className: 'flex items-center justify-between gap-3',
        children: [
          jsx('span', { className: 'text-xs font-medium text-(--ui-text-secondary)', children: label }),
          jsx(PairWordCounter, { typed, want })
        ]
      }),
      input,
      hint ? text(ui.hint, hint) : null
    ]
  })
}

function PairWordsInput({ value, placeholder, onChange, onEnter }) {
  return jsx(Input, {
    value,
    placeholder,
    size: 'lg',
    className: 'px-3.5 py-2.5 font-mono text-base tracking-wide',
    autoFocus: true,
    autoComplete: 'off',
    spellCheck: false,
    onChange: e => onChange(e.target.value),
    onKeyDown: onEnter
  })
}

function PairFooter({ children }) {
  return jsx('div', {
    className: 'flex flex-wrap items-center justify-between gap-3 border-t border-(--ui-stroke-tertiary) pt-4',
    children
  })
}

function BigWords({ words }) {
  const columns = words.length === 6 ? 3 : Math.min(Math.max(words.length, 1), 4)
  return jsx('ol', {
    className: 'grid gap-3',
    style: { gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))` },
    children: words.map((w, i) =>
      jsxs('li', {
        className: 'flex min-w-0 flex-col items-center gap-1 rounded-lg px-3 pb-4 pt-2.5',
        style: { background: pairNeutral(3), boxShadow: `inset 0 0 0 1px ${pairNeutral(10)}` },
        children: [
          jsx('span', {
            className: 'text-[0.6875rem] font-medium tabular-nums text-(--ui-text-tertiary)',
            children: i + 1
          }),
          // The demo recorder reads the words with `span.font-mono.text-xl`: keep both classes and only
          // the word in this span. The inline size is what actually renders.
          jsx('span', {
            className: 'font-mono text-xl font-semibold tracking-wider text-(--ui-text-primary)',
            style: { fontSize: '1.5rem', lineHeight: '2rem' },
            children: w.toUpperCase()
          })
        ]
      }, `${i}-${w}`)
    )
  })
}

function Countdown({ seconds }) {
  const [left, setLeft] = useState(seconds)
  useEffect(() => setLeft(seconds), [seconds])
  useEffect(() => {
    const timer = setInterval(() => setLeft(s => (typeof s === 'number' ? Math.max(0, s - 1) : s)), 1000)
    return () => clearInterval(timer)
  }, [])
  if (typeof left !== 'number') return null
  const m = Math.floor(left / 60)
  const s = String(left % 60).padStart(2, '0')
  const low = left <= 60
  const percent = Math.min(100, (left / Math.max(PAIR_TTL_SECONDS, left)) * 100)
  return jsxs('div', {
    className: 'flex w-full items-center gap-3',
    children: [
      jsxs('span', {
        className: 'inline-flex shrink-0 items-center gap-1.5 rounded-full px-2.5 py-1 text-xs tabular-nums',
        style: {
          background: low ? pairTint('--ui-yellow', 14) : pairNeutral(6),
          color: low ? 'var(--ui-yellow)' : 'var(--ui-text-secondary)'
        },
        children: [jsx(Codicon, { name: 'watch', size: 12 }), `${m}:${s} left`]
      }),
      jsx('div', {
        className: 'h-1 flex-1 overflow-hidden rounded-full',
        style: { background: pairNeutral(8) },
        children: jsx('div', {
          className: 'h-full rounded-full',
          style: {
            width: `${percent}%`,
            background: low ? 'var(--ui-yellow)' : 'var(--ui-accent)',
            transition: 'width 1s linear'
          }
        })
      })
    ]
  })
}

function usePairingSession(session, setSession) {
  const tick = useRefresh()
  useEffect(() => {
    if (!session || ['paired', 'mismatch', 'failed'].includes(session.state)) return undefined
    let live = true
    const load = () =>
      rest(`/pair/${session.session}`)
        .then(next => live && setSession(prev => ({ ...prev, ...next })))
        .catch(() => {})
    load()
    const timer = setInterval(load, 2000)
    return () => {
      live = false
      clearInterval(timer)
    }
  }, [session && session.session, session && session.state, tick])
}

function PairOutcomePanel({ tone, icon, title, titleClass = ui.heading, children }) {
  const color = tone ? `--ui-${tone}` : '--ui-text-tertiary'
  return jsxs('div', {
    className: 'flex items-start gap-4 rounded-lg p-4',
    style: tone
      ? { background: pairTint(color, 8), boxShadow: `inset 0 0 0 1px ${pairTint(color, 26)}` }
      : { background: pairNeutral(4) },
    children: [
      jsx('span', {
        className: 'grid size-10 shrink-0 place-items-center rounded-full',
        style: { background: pairTint(color, 16), color: `var(${color})` },
        children: jsx(Codicon, { name: icon, size: 20 })
      }),
      jsxs('div', {
        className: 'flex min-w-0 flex-col gap-1.5 pt-0.5',
        children: [text(titleClass, title), children]
      })
    ]
  })
}

function Outcome({ session, cancelled = false }) {
  if (session.state === 'paired' && session.peer) {
    return jsx(PairOutcomePanel, {
      tone: 'green',
      icon: 'check',
      title: `Paired with ${session.peer.name}`,
      children: jsxs('div', {
        className: 'flex flex-col gap-2',
        children: [
          text(ui.body, 'You can message each other now. Find them under Peers.'),
          jsx('span', {
            className: 'w-fit max-w-full break-all rounded-md px-2 py-1 font-mono text-xs text-(--ui-text-secondary)',
            style: { background: pairNeutral(6) },
            children: session.peer.peer_id
          })
        ]
      })
    })
  }
  if (session.state === 'mismatch' && cancelled) {
    return jsx(PairOutcomePanel, {
      icon: 'circle-slash',
      title: 'Cancelled',
      children: text(ui.body, "Those words can't be used any more. Start again whenever you're ready.")
    })
  }
  if (session.state === 'mismatch') {
    return jsx(PairOutcomePanel, {
      tone: 'red',
      icon: 'warning',
      title: 'Not paired',
      titleClass: `text-base font-semibold ${ui.bad}`,
      children: text(
        ui.body,
        'Someone may be in the middle; everything was discarded. Start again, and compare the words carefully.'
      )
    })
  }
  if (session.state === 'failed') {
    return jsx(PairOutcomePanel, {
      tone: 'red',
      icon: 'error',
      title: "Pairing didn't finish",
      children: text(ui.body, session.error || 'Pairing failed.')
    })
  }
  return null
}

function PairTab() {
  const [session, setSession] = useState(null)
  const [words, setWords] = useState(null)
  const [typed, setTyped] = useState('')
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [cancelled, setCancelled] = useState(false)
  usePairingSession(session, setSession)

  useEffect(() => {
    if (session && session.state !== 'waiting') setWords(null)
  }, [session && session.state])
  useEffect(() => () => setWords(null), [])

  const start = () => {
    setBusy(true)
    setNote('')
    setCancelled(false)
    rest('/pair/start', { method: 'POST', body: {} })
      .then(res => {
        setWords(res.words)
        const { words: _drop, ...rest_ } = res
        setSession(rest_)
      })
      .catch(err => setNote(errorText(err)))
      .finally(() => setBusy(false))
  }
  const verify = () => {
    setBusy(true)
    rest(`/pair/${session.session}/verify`, { method: 'POST', body: { words: typed } })
      .then(res => {
        setSession(res)
        setNote(
          res.match
            ? ''
            : "Those words don't match. Check what you typed. If they really differ, stop: someone may be in the middle."
        )
        if (res.match) setTyped('')
        emit()
      })
      .catch(err => setNote(errorText(err)))
      .finally(() => setBusy(false))
  }
  const abort = () =>
    rest(`/pair/${session.session}/abort`, { method: 'POST' }).then(res => setSession(s => ({ ...s, ...res })))
  const cancel = () => {
    setCancelled(true)
    abort()
  }

  if (!session || PAIR_ENDED.includes(session.state)) {
    const paired = Boolean(session && session.state === 'paired')
    return jsx(PairLayout, {
      steps: !session || paired ? PAIR_STEPS : null,
      current: paired ? PAIR_STEPS.length : 0,
      children: jsxs(PairBody, {
        children: [
          session
            ? jsx(Outcome, { session, cancelled })
            : jsx(PairHeader, {
                title: 'Pair with someone',
                subtitle:
                  "You'll get six words to read to the other person, on a call or in person. Then you both check four safety words."
              }),
          jsx(PairNote, { children: note }),
          jsxs('div', {
            className: 'flex flex-wrap items-center justify-between gap-3',
            children: [
              jsx('div', {
                className: ui.actions,
                children: jsx(Button, {
                  size: 'lg',
                  variant: paired ? 'secondary' : 'default',
                  onClick: start,
                  loading: busy,
                  children: session ? 'Pair again' : 'Start pairing'
                })
              }),
              session ? null : text(ui.hint, 'The words work once and expire after 10 minutes.')
            ]
          })
        ]
      })
    })
  }
  if (session.state === 'waiting' || session.state === 'starting') {
    return jsx(PairLayout, {
      steps: PAIR_STEPS,
      current: 0,
      children: jsxs(PairBody, {
        children: [
          jsx(PairHeader, {
            title: 'Your invitation words (one use)',
            subtitle:
              "Read these words to the other person on a call or in person. Don't paste them in a chat, or anywhere an agent can read them."
          }),
          words ? jsx(BigWords, { words }) : jsx(PairStatus, { children: 'Getting your words…' }),
          jsx(Countdown, { seconds: session.seconds_left }),
          jsxs(PairFooter, {
            children: [
              jsx(PairStatus, { children: 'Waiting for the other person to join…' }),
              jsx('div', {
                className: ui.actions,
                children: jsx(Button, { variant: 'outline', onClick: cancel, children: 'Cancel' })
              })
            ]
          })
        ]
      })
    })
  }
  const ready = Boolean(typed.trim()) && !busy
  return jsx(PairLayout, {
    steps: PAIR_STEPS,
    current: 1,
    children: jsxs(PairBody, {
      children: [
        jsx(PairHeader, {
          title: 'They joined. Now compare safety words.',
          subtitle: 'Ask them to read you the 4 safety words on their screen, and type them here.'
        }),
        jsx(PairField, {
          label: 'Safety words',
          typed,
          want: 4,
          hint: 'Separate them with spaces.',
          input: jsx(PairWordsInput, {
            value: typed,
            placeholder: 'four safety words',
            onChange: value => {
              setTyped(value)
              setNote('')
            },
            onEnter: pairOnEnter(ready, verify)
          })
        }),
        jsx(PairNote, { children: note }),
        jsxs('div', {
          className: ui.actions,
          children: [
            jsx(Button, { size: 'lg', onClick: verify, disabled: !typed.trim(), loading: busy, children: 'Verify' }),
            jsxs(Button, {
              size: 'lg',
              variant: 'ghost',
              className: ui.bad,
              onClick: abort,
              children: [jsx(Codicon, { name: 'stop-circle' }), "They don't match — stop"]
            })
          ]
        }),
        jsx(PairFooter, { children: jsx(Countdown, { seconds: session.seconds_left }) })
      ]
    })
  })
}

function JoinTab() {
  const [session, setSession] = useState(null)
  const [typed, setTyped] = useState('')
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  usePairingSession(session, setSession)

  const join = () => {
    setNote('')
    setBusy(true)
    rest('/join/start', { method: 'POST', body: { words: typed } })
      .then(res => {
        setTyped('')
        setSession(res)
      })
      .catch(err => setNote(errorText(err)))
      .finally(() => setBusy(false))
  }
  const answer = match => {
    setBusy(true)
    rest(`/pair/${session.session}/confirm`, { method: 'POST', body: { match } })
      .then(res => {
        setSession(res)
        emit()
      })
      .catch(err => setNote(errorText(err)))
      .finally(() => setBusy(false))
  }

  if (session && PAIR_ENDED.includes(session.state)) {
    const paired = session.state === 'paired'
    return jsx(PairLayout, {
      steps: paired ? PAIR_JOIN_STEPS : null,
      current: PAIR_JOIN_STEPS.length,
      children: jsxs(PairBody, {
        children: [
          jsx(Outcome, { session }),
          jsx('div', {
            className: ui.actions,
            children: jsx(Button, {
              size: 'lg',
              variant: paired ? 'secondary' : 'default',
              onClick: () => {
                setSession(null)
                setNote('')
              },
              children: 'Join again'
            })
          })
        ]
      })
    })
  }
  if (!session) {
    const ready = Boolean(typed.trim()) && !busy
    return jsx(PairLayout, {
      steps: PAIR_JOIN_STEPS,
      current: 0,
      children: jsxs(PairBody, {
        children: [
          jsx(PairHeader, {
            title: 'Join with six words',
            subtitle: 'Type the six words the other person reads to you, on a call or in person.'
          }),
          jsx(PairField, {
            label: 'Invitation words',
            typed,
            want: 6,
            hint: 'Separate them with spaces. The words work once.',
            input: jsx(PairWordsInput, {
              value: typed,
              placeholder: 'six words',
              onChange: value => {
                setTyped(value)
                setNote('')
              },
              onEnter: pairOnEnter(ready, join)
            })
          }),
          jsx(PairNote, { children: note }),
          jsx('div', {
            className: ui.actions,
            children: jsx(Button, { size: 'lg', onClick: join, disabled: !typed.trim(), loading: busy, children: 'Join' })
          })
        ]
      })
    })
  }
  if (session.state !== 'verifying') {
    return jsx(PairLayout, {
      steps: PAIR_JOIN_STEPS,
      current: 1,
      children: jsxs(PairBody, {
        children: [
          jsx(PairHeader, {
            title: 'Connecting to the other person',
            subtitle: 'Keep this page open. It usually takes a few seconds.'
          }),
          jsx(PairStatus, { children: 'Looking for the other side…' }),
          jsx(PairFooter, { children: jsx(Countdown, { seconds: session.seconds_left }) })
        ]
      })
    })
  }
  return jsx(PairLayout, {
    steps: PAIR_JOIN_STEPS,
    current: 1,
    children: jsxs(PairBody, {
      children: [
        jsx(PairHeader, {
          title: 'Read these 4 safety words aloud to the other person:',
          subtitle: 'They type them on their side. Answer only after they tell you whether the words match.'
        }),
        jsx(BigWords, { words: (session.safety_words || '').split(' ').filter(Boolean) }),
        jsx(PairNote, { children: note }),
        jsxs('div', {
          className: ui.actions,
          children: [
            jsx(Button, { size: 'lg', onClick: () => answer(true), disabled: busy, children: 'They match' }),
            jsx(Button, {
              size: 'lg',
              variant: 'destructive',
              onClick: () => answer(false),
              disabled: busy,
              children: "They don't match"
            })
          ]
        }),
        jsx(PairFooter, { children: jsx(Countdown, { seconds: session.seconds_left }) })
      ]
    })
  })
}

function useList(path, key) {
  const tick = useRefresh()
  const [items, setItems] = useState(null)
  const [error, setError] = useState('')
  const load = useCallback(() => {
    rest(path)
      .then(res => {
        setItems(key ? res[key] || [] : res)
        setError('')
      })
      .catch(err => setError(errorText(err)))
  }, [path, key])
  useEffect(load, [load, tick])
  return [items, error, load]
}

// Disk plugins are not scanned by Tailwind, so tints and one-off sizes go in inline styles,
// built from theme variables only.
const ListMix = (variable, percent) => `color-mix(in srgb, var(${variable}) ${percent}%, transparent)`

const ListTones = {
  good: { color: '--ui-green', icon: 'pass-filled' },
  bad: { color: '--ui-red', icon: 'error' },
  warn: { color: '--ui-yellow', icon: 'warning' },
  info: { color: '--ui-accent', icon: 'info' }
}

const ListUntrustedHint =
  'Content from another person or agent. Your agent treats it as data and never acts on it by itself.'

function ListBox(tag, props) {
  return Array.isArray(props.children) ? jsxs(tag, props) : jsx(tag, props)
}

function ListSurface({ as = 'section', dim = false, className = '', children }) {
  return ListBox(as, {
    className: `flex flex-col gap-4 rounded-xl border border-(--ui-stroke-secondary) p-5 shadow-xs ${className}`,
    style: { background: 'var(--ui-bg-elevated)', opacity: dim ? 0.7 : undefined },
    children
  })
}

function ListAvatar({ name, size = 40, quiet = false }) {
  const initial = (String(name || '?').trim()[0] || '?').toUpperCase()
  return jsx('span', {
    'aria-hidden': true,
    className: 'grid shrink-0 place-items-center rounded-full font-semibold',
    style: {
      width: size,
      height: size,
      fontSize: Math.round(size * 0.4),
      background: quiet ? 'var(--ui-bg-tertiary)' : ListMix('--ui-accent', 14),
      color: quiet ? 'var(--ui-text-tertiary)' : 'var(--ui-accent)'
    },
    children: initial
  })
}

function ListWhen({ ts, prefix = '' }) {
  if (!ts) return null
  return jsx('span', {
    className: 'shrink-0 text-xs tabular-nums text-(--ui-text-tertiary)',
    title: new Date(ts * 1000).toLocaleString(),
    children: `${prefix ? `${prefix} ` : ''}${relativeTime(ts * 1000)}`
  })
}

function ListNotice({ tone = 'info', children }) {
  const t = ListTones[tone] || ListTones.info
  return jsxs('div', {
    role: tone === 'bad' ? 'alert' : 'status',
    className: 'flex items-start gap-2.5 rounded-lg border px-3.5 py-2.5 text-sm leading-5 text-(--ui-text-primary)',
    style: { background: ListMix(t.color, 9), borderColor: ListMix(t.color, 32) },
    children: [
      jsx(Codicon, { name: t.icon, className: 'mt-0.5 shrink-0', style: { color: `var(${t.color})` } }),
      ListBox('div', { className: 'flex min-w-0 flex-1 flex-col gap-0.5', children })
    ]
  })
}

function ListIntro({ icon, children }) {
  return jsxs('div', {
    className: 'flex items-start gap-2 px-1 text-xs leading-5 text-(--ui-text-tertiary)',
    children: [jsx(Codicon, { name: icon, className: 'mt-0.5 shrink-0' }), jsx('span', { children })]
  })
}

function ListEmpty({ icon, title, description }) {
  return jsxs('div', {
    className:
      'flex flex-col items-center gap-3 rounded-xl border border-dashed border-(--ui-stroke-secondary) px-6 py-12 text-center',
    children: [
      jsx('span', {
        className: 'grid size-11 place-items-center rounded-full text-(--ui-text-tertiary)',
        style: { background: 'var(--ui-bg-tertiary)' },
        children: jsx(Codicon, { name: icon, size: 18 })
      }),
      jsxs('div', {
        className: 'flex flex-col items-center gap-1',
        children: [
          text('text-sm font-medium text-(--ui-text-primary)', title),
          description ? text('max-w-sm text-xs leading-5 text-(--ui-text-tertiary)', description) : null
        ]
      })
    ]
  })
}

function ListLoading() {
  return text('px-1 py-8 text-center text-sm text-(--ui-text-tertiary)', 'Loading…')
}

function ListWords({ words }) {
  return jsx('div', {
    className: 'flex flex-wrap gap-2',
    children: words.map((w, i) =>
      jsx('span', {
        className:
          'rounded-md border border-(--ui-stroke-secondary) px-3 py-1.5 font-mono text-sm font-medium tracking-wider text-(--ui-text-primary)',
        style: { background: 'var(--ui-bg-elevated)' },
        children: w.toUpperCase()
      }, `${i}-${w}`)
    )
  })
}

function ListUntrusted() {
  return jsx(Tip, {
    label: ListUntrustedHint,
    children: jsx('span', {
      tabIndex: 0,
      'aria-label': `Untrusted. ${ListUntrustedHint}`,
      className: 'inline-flex cursor-help rounded-[3px]',
      children: jsxs(Badge, {
        variant: 'outline',
        children: [jsx(Codicon, { name: 'shield', size: 10 }), 'Untrusted']
      })
    })
  })
}

function ListPeerCard({ peer, words, armed, busy, onShow, onHide, onArm, onCancel, onRevoke }) {
  const active = peer.status === 'active'
  const fingerprint = words
    ? jsxs('div', {
        className: 'flex flex-col gap-3 rounded-lg p-4',
        style: { background: 'var(--ui-bg-quaternary)' },
        children: [
          jsxs('div', {
            className: 'flex items-start justify-between gap-3',
            children: [
              text(
                'text-xs leading-5 text-(--ui-text-secondary)',
                'Safety words for this connection. To double-check, ask them to read theirs aloud: they should be the same.'
              ),
              jsx(Button, { variant: 'ghost', size: 'xs', onClick: onHide, children: 'Hide' })
            ]
          }),
          jsx(ListWords, { words })
        ]
      })
    : null
  const confirm = jsxs('div', {
    className: 'flex flex-col gap-3 rounded-lg border p-4',
    style: { background: ListMix('--ui-red', 7), borderColor: ListMix('--ui-red', 30) },
    children: [
      jsxs('div', {
        className: 'flex flex-col gap-1',
        children: [
          text('text-sm font-medium text-(--ui-text-primary)', `Revoke ${peer.name}?`),
          text(
            'text-xs leading-5 text-(--ui-text-secondary)',
            "You won't get messages from them and can't send any. To talk again you'll have to pair again."
          )
        ]
      }),
      jsxs('div', {
        className: 'flex items-center justify-end gap-2',
        children: [
          jsx(Button, { variant: 'ghost', onClick: onCancel, children: 'Cancel' }),
          jsx(Button, { variant: 'destructive', loading: busy, onClick: onRevoke, children: 'Confirm revoke' })
        ]
      })
    ]
  })
  const actions =
    active && !armed
      ? jsxs('div', {
          className: 'flex shrink-0 items-center gap-1',
          children: [
            words
              ? null
              : jsxs(Button, {
                  variant: 'outline',
                  onClick: onShow,
                  children: [jsx(Codicon, { name: 'eye' }), 'Show fingerprint']
                }),
            jsx(Button, { variant: 'ghost', className: 'text-(--ui-red)', onClick: onArm, children: 'Revoke' })
          ]
        })
      : null
  return jsxs(ListSurface, {
    dim: !active,
    children: [
      jsxs('div', {
        className: 'flex items-center gap-3',
        children: [
          jsx(ListAvatar, { name: peer.name, quiet: !active }),
          jsxs('div', {
            className: 'flex min-w-0 flex-1 flex-col gap-1',
            children: [
              jsxs('div', {
                className: 'flex items-center gap-2',
                children: [
                  jsx('span', { className: 'truncate text-sm font-semibold text-(--ui-text-primary)', children: peer.name }),
                  jsx(Badge, { variant: active ? 'success' : 'muted', children: active ? 'Active' : 'Revoked' }),
                  peer.held
                    ? jsxs(Badge, {
                        variant: 'default',
                        children: [jsx(Codicon, { name: 'mail', size: 10 }), `${peer.held} waiting`]
                      })
                    : null
                ]
              }),
              jsxs('div', {
                className: 'flex flex-wrap items-center gap-2 text-xs text-(--ui-text-tertiary)',
                children: [
                  jsx('span', { className: 'font-mono', children: peer.display_id }),
                  jsx('span', { 'aria-hidden': true, children: '·' }),
                  jsx('span', {
                    title: peer.last_seen ? new Date(peer.last_seen * 1000).toLocaleString() : undefined,
                    children: peer.last_seen ? `Last seen ${relativeTime(peer.last_seen * 1000)}` : 'Not heard from yet'
                  })
                ]
              })
            ]
          }),
          actions
        ]
      }),
      fingerprint,
      active && armed ? confirm : null
    ]
  })
}

function PeersTab() {
  const [peers, error] = useList('/peers', 'peers')
  const [armed, setArmed] = useState(null)
  const [busy, setBusy] = useState(null)
  const [fingerprint, setFingerprint] = useState({})
  const [note, setNote] = useState('')
  if (error) return jsx(ListNotice, { tone: 'bad', children: error })
  if (!peers) return jsx(ListLoading, {})
  if (!peers.length) {
    return jsx(ListEmpty, {
      icon: 'organization',
      title: 'No one paired yet',
      description: 'Use Pair or Join to connect with someone. You read a few words to each other, on a call or in person.'
    })
  }
  const show = p =>
    rest(`/peers/${p.peer_id}/fingerprint`)
      .then(res => setFingerprint(f => ({ ...f, [p.peer_id]: res.safety_words })))
      .catch(err => setNote(errorText(err)))
  const hide = p => setFingerprint(({ [p.peer_id]: _drop, ...f }) => f)
  const revoke = p => {
    setBusy(p.peer_id)
    rest(`/peers/${p.peer_id}/revoke`, { method: 'POST' })
      .then(() => {
        setArmed(null)
        emit()
      })
      .catch(err => setNote(errorText(err)))
      .finally(() => setBusy(null))
  }
  return jsxs('div', {
    className: 'flex flex-col gap-3',
    children: [
      note ? jsx(ListNotice, { tone: 'bad', children: note }) : null,
      ...peers.map(p =>
        jsx(ListPeerCard, {
          peer: p,
          words: fingerprint[p.peer_id],
          armed: armed === p.peer_id,
          busy: busy === p.peer_id,
          onShow: () => show(p),
          onHide: () => hide(p),
          onArm: () => setArmed(p.peer_id),
          onCancel: () => setArmed(null),
          onRevoke: () => revoke(p)
        }, p.peer_id)
      )
    ]
  })
}

function ListMessageCard({ message: m, busy, onDiscard }) {
  return jsxs(ListSurface, {
    as: 'article',
    className: 'gap-3',
    children: [
      jsxs('header', {
        className: 'flex items-center gap-3',
        children: [
          jsx(ListAvatar, { name: m.peer, size: 32 }),
          jsxs('div', {
            className: 'flex min-w-0 flex-1 flex-wrap items-center gap-2',
            children: [
              jsx('span', { className: 'truncate text-sm font-semibold text-(--ui-text-primary)', children: m.peer }),
              jsx(Badge, { variant: 'muted', children: m.type }),
              jsx(ListUntrusted, {})
            ]
          }),
          jsx(ListWhen, { ts: m.received_at })
        ]
      }),
      // Plain text only: no links, no images, no markup.
      jsx('p', {
        className: 'whitespace-pre-wrap break-words text-sm leading-6 text-(--ui-text-primary) select-text',
        style: { paddingLeft: 44 },
        children: m.text
      }),
      jsx('div', {
        className: 'flex justify-end',
        children: jsxs(Button, {
          variant: 'ghost',
          size: 'sm',
          loading: busy,
          onClick: onDiscard,
          children: [jsx(Codicon, { name: 'trash' }), 'Discard']
        })
      })
    ]
  })
}

function InboxTab() {
  const [messages, error] = useList('/inbox', 'messages')
  const [busy, setBusy] = useState(null)
  const [note, setNote] = useState('')
  if (error) return jsx(ListNotice, { tone: 'bad', children: error })
  if (!messages) return jsx(ListLoading, {})
  if (!messages.length) {
    return jsx(ListEmpty, {
      icon: 'inbox',
      title: 'Inbox empty',
      description: "You're all caught up. Messages from people you paired with wait here until you look at them."
    })
  }
  const discard = m => {
    const id = `${m.peer_id}-${m.id}`
    setBusy(id)
    rest(`/inbox/${m.peer_id}/${m.id}/discard`, { method: 'POST' })
      .then(() => emit())
      .catch(err => setNote(errorText(err)))
      .finally(() => setBusy(null))
  }
  return jsxs('div', {
    className: 'flex flex-col gap-3',
    children: [
      jsx(ListIntro, {
        icon: 'shield',
        children:
          'Messages from people you paired with. Your agent reads them as plain information and never acts on them by itself.'
      }),
      note ? jsx(ListNotice, { tone: 'bad', children: note }) : null,
      ...messages.map(m =>
        jsx(ListMessageCard, {
          message: m,
          busy: busy === `${m.peer_id}-${m.id}`,
          onDiscard: () => discard(m)
        }, `${m.peer_id}-${m.id}`)
      )
    ]
  })
}

function ListDraftCard({ draft: d, busy, onSend, onDiscard }) {
  const blocked = Boolean(d.leak_warning)
  return jsxs(ListSurface, {
    as: 'article',
    children: [
      jsxs('header', {
        className: 'flex items-center gap-3',
        children: [
          jsx(ListAvatar, { name: d.peer, size: 32 }),
          jsxs('div', {
            className: 'flex min-w-0 flex-1 items-center gap-2',
            children: [
              jsxs('span', {
                className: 'truncate text-sm text-(--ui-text-secondary)',
                children: ['To ', jsx('span', { className: 'font-semibold text-(--ui-text-primary)', children: d.peer })]
              }),
              jsx(Badge, { variant: 'muted', children: d.type })
            ]
          }),
          jsx(ListWhen, { ts: d.created_at, prefix: 'Drafted' })
        ]
      }),
      jsxs('div', {
        className: 'flex flex-col gap-1.5',
        children: [
          text('text-[0.6875rem] font-medium uppercase tracking-wider text-(--ui-text-tertiary)', 'Exactly what will be sent'),
          jsx('blockquote', {
            className:
              'whitespace-pre-wrap break-words rounded-lg border-l-2 border-(--ui-accent) px-4 py-3 text-sm leading-6 text-(--ui-text-primary) select-text',
            style: { background: 'var(--ui-bg-quaternary)' },
            children: d.text
          })
        ]
      }),
      blocked
        ? jsxs(ListNotice, {
            tone: 'warn',
            children: [
              jsx('div', { className: 'font-medium', children: d.leak_warning }),
              text(
                'text-xs leading-5 text-(--ui-text-secondary)',
                'Sending is turned off for this draft. Ask your agent to rewrite it, or discard it.'
              )
            ]
          })
        : null,
      jsxs('div', {
        className: 'flex items-center justify-end gap-2',
        children: [
          jsx(Button, { variant: 'ghost', onClick: onDiscard, children: 'Discard' }),
          jsxs(Button, {
            disabled: blocked,
            loading: busy,
            onClick: onSend,
            children: [jsx(Codicon, { name: 'send' }), 'Send exactly this']
          })
        ]
      })
    ]
  })
}

function DraftsTab() {
  const [drafts, error] = useList('/drafts', 'drafts')
  const [note, setNote] = useState(null)
  const [busy, setBusy] = useState(null)
  if (error) return jsx(ListNotice, { tone: 'bad', children: error })
  if (!drafts) return jsx(ListLoading, {})
  const send = d => {
    setBusy(d.id)
    setNote(null)
    rest(`/drafts/${d.id}/send`, { method: 'POST' })
      .then(res => {
        setNote({ tone: 'good', text: `Sent to ${res.peer}.` })
        emit()
      })
      .catch(err => setNote({ tone: 'bad', text: errorText(err) }))
      .finally(() => setBusy(null))
  }
  const discard = d => {
    setNote(null)
    rest(`/drafts/${d.id}/discard`, { method: 'POST' })
      .then(() => emit())
      .catch(err => setNote({ tone: 'bad', text: errorText(err) }))
  }
  const body = drafts.length
    ? [
        jsx(ListIntro, {
          icon: 'edit',
          children: 'Your agent wrote these. Nothing goes out until you send it, and it goes out exactly as shown.'
        }, 'intro'),
        ...drafts.map(d =>
          jsx(ListDraftCard, { draft: d, busy: busy === d.id, onSend: () => send(d), onDiscard: () => discard(d) }, d.id)
        )
      ]
    : [
        jsx(ListEmpty, {
          icon: 'edit',
          title: 'No drafts',
          description: 'Your agent leaves drafts here; you send them.'
        }, 'empty')
      ]
  return jsxs('div', {
    className: 'flex flex-col gap-3',
    children: [note ? jsx(ListNotice, { tone: note.tone, children: note.text }, 'note') : null, ...body]
  })
}

const ShellTabIcons = { pair: 'person-add', join: 'sign-in', peers: 'organization', inbox: 'inbox', drafts: 'edit' }

function ShellHeader() {
  return jsxs('header', {
    className: 'flex items-start gap-4',
    children: [
      jsx('span', {
        'aria-hidden': true,
        className: 'grid size-11 shrink-0 place-items-center rounded-xl text-(--ui-accent)',
        style: { background: ListMix('--ui-accent', 12) },
        children: jsx(Codicon, { name: 'shield', size: 22 })
      }),
      jsxs('div', {
        className: 'flex min-w-0 flex-col gap-1',
        children: [
          jsx('h1', { className: 'text-xl font-semibold tracking-tight text-(--ui-text-primary)', children: 'Nachalo' }),
          jsx('p', {
            className: 'text-sm leading-6 text-(--ui-text-secondary)',
            children:
              "Private, end-to-end encrypted messages between your agent and someone else's. Your agent drafts; you send."
          })
        ]
      })
    ]
  })
}

function ShellTabs({ tab, onChange, counts }) {
  const count = { peers: counts && counts.peers, inbox: counts && counts.held, drafts: counts && counts.drafts }
  return jsx('nav', {
    'aria-label': 'Nachalo sections',
    className: 'flex flex-wrap gap-1 border-b border-(--ui-stroke-tertiary)',
    children: TABS.map(t => {
      const active = t === tab
      const n = count[t]
      return jsxs('button', {
        type: 'button',
        'aria-current': active ? 'page' : undefined,
        onClick: () => onChange(t),
        className: `flex items-center gap-2 border-b-2 px-3 py-2.5 text-sm font-medium transition-colors ${
          active
            ? 'border-(--ui-accent) text-(--ui-text-primary)'
            : 'border-transparent text-(--ui-text-tertiary) hover:text-(--ui-text-primary)'
        }`,
        style: { marginBottom: -1 },
        children: [
          jsx(Codicon, { name: ShellTabIcons[t], size: 14 }),
          jsx('span', { children: TAB_LABELS[t] }),
          n
            ? jsx(Badge, {
                'aria-hidden': true,
                variant: t === 'peers' ? 'muted' : 'default',
                className: 'tabular-nums',
                children: n
              })
            : null
        ]
      }, t)
    })
  })
}

function Page() {
  const [tab, setTab] = useState('pair')
  const [counts] = useList('/status', null)
  const views = { pair: PairTab, join: JoinTab, peers: PeersTab, inbox: InboxTab, drafts: DraftsTab }
  return jsx('div', {
    className: 'h-full overflow-auto',
    children: jsxs('div', {
      className: ui.page,
      children: [
        jsx(ShellHeader, {}),
        jsx(ShellTabs, { tab, onChange: setTab, counts }),
        jsx(views[tab], {})
      ]
    })
  })
}

function Chip() {
  const tick = useRefresh()
  const [counts, setCounts] = useState(null)
  useEffect(() => {
    rest('/status').then(setCounts).catch(() => setCounts(null))
  }, [tick])
  if (!counts || (!counts.held && !counts.drafts)) return null
  const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`
  const short = [counts.held ? `${counts.held} new` : '', counts.drafts ? plural(counts.drafts, 'draft') : '']
  const long = [
    counts.held ? `${plural(counts.held, 'message')} waiting` : '',
    counts.drafts ? `${plural(counts.drafts, 'draft')} to review` : ''
  ]
  const label = `Nachalo: ${long.filter(Boolean).join(', ')}`
  return jsxs('button', {
    type: 'button',
    title: label,
    'aria-label': label,
    className:
      'flex h-full items-center gap-1 px-1.5 text-[0.6875rem] text-(--ui-text-tertiary) transition-colors hover:text-(--ui-text-primary)',
    onClick: () => host.navigate('/nachalo'),
    children: [
      jsx(Codicon, { name: 'shield', size: 11, style: counts.held ? { color: 'var(--ui-accent)' } : undefined }),
      jsx('span', { className: 'tabular-nums', children: short.filter(Boolean).join(' · ') })
    ]
  })
}

export default {
  id: ID,
  name: 'Nachalo',
  defaultEnabled: false,
  register(ctx) {
    rest = (path, opts) => ctx.rest(path, opts)
    ctx.registerMany([
      { id: 'page', area: ROUTES_AREA, data: { path: '/nachalo' }, render: () => jsx(Page, {}) },
      { id: 'nav', area: SIDEBAR_NAV_AREA, data: { path: '/nachalo', label: 'Nachalo', codicon: 'shield' } },
      { id: 'chip', area: STATUSBAR_AREAS.right, order: 140, render: () => jsx(Chip, {}) }
    ])
    for (const event of ['pairing.state', 'inbox.count', 'drafts.changed', 'peers.changed']) {
      ctx.onEvent(`plugin.${ID}.${event}`, () => emit())
    }
    ctx.onEvent(`plugin.${ID}.inbox.count`, ({ payload }) => {
      if (payload && payload.held) host.notify({ kind: 'info', message: `Nachalo: ${payload.held} held peer message(s)` })
    })
    ctx.setInterval(emit, 30000)
    ctx.onDispose(() => {
      listeners.clear()
      rest = null
    })
  }
}
