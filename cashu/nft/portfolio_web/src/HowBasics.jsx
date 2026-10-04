// Beginner version of "How it works": four illustrated ideas. Illustrations
// play when they scroll into view and replay on hover; nothing loops, so an
// idle page costs no GPU time.
import React, { useRef } from 'react';
import { motion, useAnimationControls } from 'motion/react';
import { ArrowRight, Check, X } from 'lucide-react';
import { Button } from './ui.jsx';

const ink = { stroke: 'var(--ink)', strokeWidth: 3, strokeLinejoin: 'round', strokeLinecap: 'round' };
const fill = (c) => ({ fill: c });
const label = { fontFamily: 'var(--mono)', fontWeight: 700, fontSize: 11, fill: '#111' };
const spring = { type: 'spring', stiffness: 260, damping: 20 };
const at = (delay, extra = {}) => ({ ...spring, delay, ...extra });

/** Plays its children's `hidden` → `show` variants on entering the viewport
 *  and again whenever the pointer moves onto it. */
function Scene({ children, tone }) {
  const controls = useAnimationControls();
  const busy = useRef(false);
  const play = async () => {
    if (busy.current) return;
    busy.current = true;
    controls.set('hidden');
    await controls.start('show');
    busy.current = false;
  };
  return <motion.div className={`basics-art tone-${tone}`} initial="hidden" animate={controls}
    viewport={{ amount: 0.5 }} onViewportEnter={play} onHoverStart={play}>
    <svg viewBox="0 0 320 200" role="img" aria-hidden="true">{children}</svg>
  </motion.div>;
}

function Picture({ x, y, w, h }) {
  return <g>
    <rect x={x} y={y} width={w} height={h} rx="5" style={fill('var(--blue)')} />
    <circle cx={x + w * 0.72} cy={y + h * 0.32} r={Math.min(w, h) * 0.13} style={fill('var(--orange)')} />
    <path d={`M${x} ${y + h} L${x + w * 0.35} ${y + h * 0.5} L${x + w * 0.6} ${y + h * 0.78} L${x + w * 0.78} ${y + h * 0.6} L${x + w} ${y + h} Z`} style={fill('#111')} />
  </g>;
}

/* 1 · The token rides inside the file's metadata. */
function TokenInFile() {
  return <Scene tone="lime">
    <rect x="86" y="28" width="148" height="156" rx="14" style={{ ...fill('var(--paper)'), ...ink }} />
    <motion.rect x="86" y="28" width="148" height="34" rx="14" style={ink}
      variants={{ hidden: { fill: 'var(--surface)' }, show: { fill: 'var(--yellow)', transition: { delay: 0.75 } } }} />
    <text x="100" y="50" style={label}>EXIF</text>
    <Picture x={100} y={74} w={120} h={96} />
    <motion.g variants={{ hidden: { y: -70, opacity: 0 }, show: { y: 0, opacity: 1, transition: at(0.15) } }}>
      <circle cx="206" cy="45" r="12" style={{ ...fill('var(--lime)'), ...ink }} />
      <circle cx="206" cy="45" r="5" style={{ ...fill('var(--orange)'), stroke: '#111', strokeWidth: 2 }} />
    </motion.g>
    <motion.g variants={{ hidden: { opacity: 0, x: -8 }, show: { opacity: 1, x: 0, transition: at(1) } }}>
      <path d="M220 45 L246 45" style={{ ...ink, strokeDasharray: '3 4' }} />
      <rect x="246" y="33" width="66" height="24" rx="12" style={{ ...fill('var(--paper)'), ...ink }} />
      <text x="256" y="49" style={{ ...label, fill: 'var(--text)' }}>owner</text>
    </motion.g>
  </Scene>;
}

/* 2 · A bearer NFT: carry the frame off the wall and the NFT goes with it. */
function StolenFrame() {
  return <Scene tone="orange">
    <motion.path d="M120 26 L72 66 M120 26 L168 66" style={ink}
      variants={{ hidden: { opacity: 1 }, show: { opacity: 0, transition: { delay: 0.3 } } }} />
    <circle cx="120" cy="26" r="4" style={fill('var(--ink)')} />
    <motion.rect x="62" y="62" width="116" height="90" rx="8" style={{ fill: 'none', ...ink, strokeDasharray: '6 6' }}
      variants={{ hidden: { opacity: 0 }, show: { opacity: 0.5, transition: { delay: 0.6 } } }} />
    <motion.g variants={{ hidden: { x: 0, y: 0, rotate: 0 }, show: { x: 128, y: 14, rotate: 9, transition: at(0.35, { stiffness: 120, damping: 16 }) } }}
      style={{ transformBox: 'fill-box', transformOrigin: 'center' }}>
      <rect x="62" y="62" width="116" height="90" rx="8" style={{ ...fill('#111'), ...ink }} />
      <Picture x={72} y={72} w={96} h={70} />
    </motion.g>
    <rect x="10" y="168" width="190" height="24" rx="12" style={{ ...fill('var(--paper)'), ...ink }} />
    <text x="22" y="184" style={{ ...label, fill: 'var(--text)' }}>owner:</text>
    <motion.text x="74" y="184" style={label}
      variants={{ hidden: { opacity: 1 }, show: { opacity: 0, transition: { delay: 1.1, duration: 0.15 } } }}>
      <tspan style={{ fill: 'var(--text)' }}>you</tspan></motion.text>
    <motion.text x="74" y="184" style={label}
      variants={{ hidden: { opacity: 0 }, show: { opacity: 1, transition: { delay: 1.25, duration: 0.2 } } }}>
      <tspan style={{ fill: 'var(--orange)' }}>whoever has it</tspan></motion.text>
  </Scene>;
}

/* 3 · The mint is blindfolded: it only marks a one-time code as used. */
function BlindMint() {
  return <Scene tone="blue">
    <circle cx="30" cy="96" r="16" style={{ ...fill('var(--lime)'), ...ink }} />
    <circle cx="290" cy="96" r="16" style={{ ...fill('var(--pink)'), ...ink }} />
    <motion.g variants={{ hidden: { x: 0, opacity: 0 }, show: { x: 210, opacity: [0, 1, 1, 1, 0], transition: { delay: 0.2, duration: 1.4, ease: 'easeInOut' } } }}>
      <rect x="40" y="84" width="34" height="24" rx="4" style={{ ...fill('var(--yellow)'), ...ink }} />
      <path d="M40 86 L57 98 L74 86" style={{ ...ink, fill: 'none', strokeWidth: 2.5 }} />
    </motion.g>
    <path d="M104 74 L160 40 L216 74 Z" style={{ ...fill('var(--paper)'), ...ink }} />
    <rect x="110" y="74" width="100" height="78" rx="8" style={{ ...fill('var(--paper)'), ...ink }} />
    <text x="139" y="138" style={{ ...label, fill: 'var(--text)', fontSize: 13 }}>MINT</text>
    <motion.g variants={{ hidden: { scaleX: 0.2, opacity: 0 }, show: { scaleX: 1, opacity: 1, transition: at(0) } }} style={{ transformBox: 'fill-box', transformOrigin: 'center' }}>
      <rect x="104" y="92" width="112" height="16" rx="8" style={fill('#111')} />
      <path d="M216 100 L232 92 M216 100 L230 110" style={{ ...ink, stroke: '#111' }} />
    </motion.g>
    <rect x="188" y="160" width="112" height="30" rx="6" style={{ ...fill('var(--paper)'), ...ink }} />
    <text x="198" y="179" style={{ ...label, fill: 'var(--text)' }}>code 7f3a…</text>
    <motion.g variants={{ hidden: { scale: 2, opacity: 0, rotate: -20 }, show: { scale: 1, opacity: 1, rotate: -12, transition: at(1.45, { stiffness: 500, damping: 18 }) } }}
      style={{ transformBox: 'fill-box', transformOrigin: 'center' }}>
      <rect x="262" y="150" width="52" height="22" rx="4" style={{ ...fill('var(--orange)'), stroke: '#111', strokeWidth: 2.5 }} />
      <text x="272" y="165" style={label}>used</text>
    </motion.g>
  </Scene>;
}

/* 4 · USB stick, email or link: when your friend adds it, it's theirs. */
function SendAnyWay() {
  const file = (x, y) => <g transform={`translate(${x} ${y})`}>
    <rect x="-16" y="-20" width="32" height="40" rx="5" style={{ ...fill('var(--paper)'), ...ink }} />
    <Picture x={-11} y={-9} w={22} h={22} />
  </g>;
  return <Scene tone="pink">
    <path d="M60 100 C110 40 210 40 260 100" style={{ ...ink, fill: 'none', strokeDasharray: '4 6', opacity: 0.5 }} />
    <path d="M60 100 L260 100" style={{ ...ink, fill: 'none', strokeDasharray: '4 6', opacity: 0.5 }} />
    <path d="M60 100 C110 160 210 160 260 100" style={{ ...ink, fill: 'none', strokeDasharray: '4 6', opacity: 0.5 }} />
    <g transform="translate(160 54)"><rect x="-16" y="-9" width="26" height="18" rx="4" style={{ ...fill('var(--lime)'), ...ink }} /><rect x="10" y="-5" width="9" height="10" style={{ ...fill('var(--paper)'), ...ink, strokeWidth: 2 }} /></g>
    <g transform="translate(160 100)"><rect x="-15" y="-10" width="30" height="20" rx="3" style={{ ...fill('var(--yellow)'), ...ink }} /><path d="M-15 -8 L0 3 L15 -8" style={{ ...ink, fill: 'none', strokeWidth: 2.5 }} /></g>
    <g transform="translate(160 146)"><rect x="-17" y="-7" width="18" height="14" rx="7" style={{ fill: 'none', ...ink }} /><rect x="-1" y="-7" width="18" height="14" rx="7" style={{ fill: 'none', ...ink }} /></g>
    <motion.g variants={{ hidden: { opacity: 1 }, show: { opacity: 0.3, transition: { delay: 1.2 } } }}>{file(36, 100)}</motion.g>
    <motion.path d="M24 88 L48 112" style={{ ...ink, stroke: 'var(--orange)', strokeWidth: 4 }}
      variants={{ hidden: { pathLength: 0 }, show: { pathLength: 1, transition: { delay: 1.3, duration: 0.25 } } }} />
    <motion.g variants={{ hidden: { x: 0, opacity: 0 }, show: { x: 224, opacity: [0, 1, 1, 0], transition: { delay: 0.2, duration: 1.1, ease: 'easeInOut' } } }}>{file(36, 100)}</motion.g>
    <motion.g variants={{ hidden: { opacity: 0, scale: 0.6 }, show: { opacity: 1, scale: 1, transition: at(1.2) } }} style={{ transformBox: 'fill-box', transformOrigin: 'center' }}>
      {file(284, 100)}
      <rect x="264" y="76" width="40" height="48" rx="8" style={{ fill: 'none', stroke: 'var(--lime)', strokeWidth: 4 }} />
    </motion.g>
  </Scene>;
}

const CARDS = [
  {
    art: TokenInFile, title: 'Your NFT lives inside the file',
    body: <>When you add a picture to your collection, the mint issues an anonymous Cashu ecash token for it. When you save the NFT to send it, that token is tucked into the file’s <em>EXIF header</em>, the hidden notes every photo carries, like the camera model and date. The picture looks exactly the same. The token inside decides who owns it.</>,
  },
  {
    art: StolenFrame, title: 'Whoever has the file, has the NFT',
    body: <>It’s a real bearer NFT, like a banknote. Put the file in a digital frame on your wall, and if someone walks off with it and adds it to their wallet first, it’s theirs now. Keep it safe like cash. Until someone claims it, you can cancel, and every copy you handed out stops working.</>,
  },
  {
    art: BlindMint, title: 'Private by default',
    crypto: true,
    body: <>When you send an NFT, the mint doesn’t learn which picture moved, or who gave it to whom. All it does is stop the same NFT being spent twice: it checks a one-time code and marks it as used. Showing an NFT on your public collection is your choice.</>,
  },
  {
    art: SendAnyWay, title: 'Send it any way you like',
    body: <>Mint any JPG or PNG and save it to your computer. To give it to a friend, hand it over on a USB stick, attach it to an email, or share a link. When your friend adds the file to their wallet, it becomes theirs and stops being yours: the mint retires your copy for good. Tip: send the file itself, since screenshots and chat apps strip out the token.</>,
  },
];

const COMPARE = [
  ['Where the art lives', 'A link to a file hosted somewhere else', 'Inside the file you hold'],
  ['Who sees your trades', 'Everyone, forever, on a public ledger', 'Only you and the person you send it to'],
  ['How you send it', 'A wallet, a network and a transaction', 'A file: USB stick, email or link'],
  ['What it costs', 'Fees for every move', 'Free to mint and to send'],
];

export default function Basics({ onStart, onCrypto }) {
  return <div className="basics">
    <div className="basics-grid">
      {CARDS.map(({ art: Art, title, body, crypto }, i) => <motion.section key={title} className="basics-card"
        initial={{ opacity: 0, y: 18 }} whileInView={{ opacity: 1, y: 0 }} viewport={{ once: true, margin: '-40px' }}
        transition={{ delay: (i % 2) * 0.08, type: 'spring', stiffness: 220, damping: 26 }}>
        <Art />
        <div className="basics-copy">
          <h2><span className="mono">{String(i + 1).padStart(2, '0')}</span>{title}</h2>
          <p>{body}</p>
          {crypto && <button className="link basics-link" onClick={onCrypto}>How? Some seriously clever cryptography <ArrowRight size={14} /></button>}
        </div>
      </motion.section>)}
    </div>

    <section className="basics-compare">
      <h2>Why we think it’s better <span className="muted">(humbly)</span></h2>
      <div className="compare-table" role="table">
        <div className="compare-row compare-head" role="row"><span role="columnheader" /><span role="columnheader">Most NFTs</span><span role="columnheader">Nonfungible.cash</span></div>
        {COMPARE.map(([what, them, us]) => <div key={what} className="compare-row" role="row">
          <span role="rowheader">{what}</span>
          <span role="cell" className="them"><X size={15} />{them}</span>
          <span role="cell" className="us"><Check size={15} />{us}</span>
        </div>)}
      </div>
      <p className="hint">Experimental, unaudited cryptography. Keep a backup of your collection key.</p>
    </section>

    <div className="how-cta"><p>Ready to mint your first one?</p>
      <div className="row"><Button variant="secondary" onClick={onCrypto}>Read the cryptography</Button><Button variant="primary" icon={<ArrowRight size={16} />} onClick={onStart}>Create a collection</Button></div>
    </div>
  </div>;
}
