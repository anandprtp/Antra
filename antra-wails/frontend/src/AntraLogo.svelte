<script lang="ts">
  import { onMount } from 'svelte';

  const SIZE  = 420;
  const CX    = SIZE / 2;
  const CY    = SIZE / 2 + 10;
  const ORBIT = 158;

  const SERVICES = [
    { id: 'spotify',    label: 'Spotify',      color: '#1DB954', glowColor: 'rgba(29,185,84,0.6)'   },
    { id: 'applemusic', label: 'Apple Music',  color: '#fc3c44', glowColor: 'rgba(252,60,68,0.6)'   },
    { id: 'deezer',     label: 'Deezer',       color: '#a050e0', glowColor: 'rgba(160,80,224,0.6)'  },
    { id: 'search',     label: 'Search',       color: '#aaaaaa', glowColor: 'rgba(200,200,200,0.4)' },
    { id: 'download',   label: 'Download',     color: '#00d4d4', glowColor: 'rgba(0,212,212,0.6)'   },
    { id: 'link',       label: 'URL Import',   color: '#4a80ff', glowColor: 'rgba(74,128,255,0.6)'  },
    { id: 'amazon',     label: 'Amazon Music', color: '#1A73E8', glowColor: 'rgba(26,115,232,0.6)'  },
    { id: 'tidal',      label: 'Tidal',        color: '#dddddd', glowColor: 'rgba(220,220,220,0.4)' },
  ] as const;

  type Service = typeof SERVICES[number];

  const NUM      = SERVICES.length;
  const HOLD_MS  = 1800;
  const EASE_MS  = 700;
  const STEP_MS  = HOLD_MS + EASE_MS;

  function easeInOut(t: number) {
    return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
  }

  let angles: number[]  = SERVICES.map((_, i) => -Math.PI / 2 + (2 * Math.PI * i) / NUM);
  let activeIdx         = 0;
  let activeLabel: Service['label'] = SERVICES[0].label;
  let labelOpacity      = 1;
  let startTs: number | null = null;
  let raf: number;

  function loop(ts: number) {
    if (startTs === null) startTs = ts;
    const elapsed     = ts - startTs;
    const totalCycle  = STEP_MS * NUM;
    const t           = elapsed % totalCycle;
    const step        = Math.floor(t / STEP_MS);
    const stepT       = (t % STEP_MS) / STEP_MS;
    const easeFrac    = EASE_MS / STEP_MS;

    const extraAngle = stepT < easeFrac
      ? easeInOut(stepT / easeFrac) * (2 * Math.PI / NUM)
      : 2 * Math.PI / NUM;

    const baseAngle = (step / NUM) * 2 * Math.PI + extraAngle;

    angles = SERVICES.map((_, i) =>
      -Math.PI / 2 + (2 * Math.PI * i) / NUM + baseAngle
    );

    let closestIdx  = 0;
    let closestDist = Infinity;
    angles.forEach((a, i) => {
      const norm = ((a % (2 * Math.PI)) + 2 * Math.PI) % (2 * Math.PI);
      const d    = Math.min(
        Math.abs(norm - 3 * Math.PI / 2),
        2 * Math.PI - Math.abs(norm - 3 * Math.PI / 2)
      );
      if (d < closestDist) { closestDist = d; closestIdx = i; }
    });

    if (closestIdx !== activeIdx) {
      activeIdx   = closestIdx;
      activeLabel = SERVICES[closestIdx].label;
    }

    labelOpacity = stepT < easeFrac
      ? 1 - easeInOut(stepT / easeFrac)
      : easeInOut((stepT - easeFrac) / (1 - easeFrac));

    raf = requestAnimationFrame(loop);
  }

  onMount(() => {
    raf = requestAnimationFrame(loop);
    return () => cancelAnimationFrame(raf);
  });

  $: iconData = SERVICES.map((svc, i) => {
    const a       = angles[i];
    const x       = CX + ORBIT * Math.cos(a);
    const y       = CY + ORBIT * Math.sin(a);
    const norm    = ((a % (2 * Math.PI)) + 2 * Math.PI) % (2 * Math.PI);
    const dist    = Math.min(
      Math.abs(norm - 3 * Math.PI / 2),
      2 * Math.PI - Math.abs(norm - 3 * Math.PI / 2)
    );
    const spotlight = Math.max(0, 1 - dist / 0.55);
    const scale     = 1 + 0.45 * spotlight;
    const opacity   = 0.45 + 0.55 * (1 - Math.min(dist / Math.PI, 1));
    return { ...svc, x, y, scale, opacity, spotlight, dist };
  });

  $: sorted        = [...iconData].sort((a, b) => b.dist - a.dist);
  $: activeService = SERVICES[activeIdx] as Service;
</script>

<div style="display:flex;flex-direction:column;align-items:center;user-select:none;">
  <svg
    width={SIZE}
    height={SIZE}
    viewBox="0 0 {SIZE} {SIZE}"
    style="overflow:visible;"
  >
    <defs>
      <radialGradient id="ag-orbitGlow" cx="50%" cy="50%" r="50%">
        <stop offset="0%"   stop-color="rgba(0,212,212,0.15)"/>
        <stop offset="100%" stop-color="rgba(0,0,0,0)"/>
      </radialGradient>
      <linearGradient id="ag-arcGrad" x1="0%" y1="0%" x2="100%" y2="100%">
        <stop offset="0%"   stop-color="#00d4d4"/>
        <stop offset="40%"  stop-color="#ff4da6"/>
        <stop offset="80%"  stop-color="#9b59ff"/>
        <stop offset="100%" stop-color="#00d4d4"/>
      </linearGradient>
      <filter id="ag-logoGlow" x="-30%" y="-30%" width="160%" height="160%">
        <feGaussianBlur stdDeviation="8" result="blur"/>
        <feMerge><feMergeNode in="blur"/><feMergeNode in="SourceGraphic"/></feMerge>
      </filter>
      <filter id="ag-iconGlow" x="-50%" y="-50%" width="200%" height="200%">
        <feGaussianBlur stdDeviation="5" result="blur"/>
        <feMerge><feMergeNode in="blur"/><feMergeNode in="SourceGraphic"/></feMerge>
      </filter>
      <filter id="ag-spotGlow" x="-80%" y="-80%" width="260%" height="260%">
        <feGaussianBlur stdDeviation="10" result="blur"/>
        <feMerge><feMergeNode in="blur"/><feMergeNode in="SourceGraphic"/></feMerge>
      </filter>
    </defs>

    <!-- Orbit background glow -->
    <circle cx={CX} cy={CY} r={ORBIT} fill="url(#ag-orbitGlow)"/>

    <!-- Dashed orbit track -->
    <circle cx={CX} cy={CY} r={ORBIT}
      fill="none"
      stroke="rgba(0,212,212,0.18)"
      stroke-width="1.5"
      stroke-dasharray="6 6"
    />

    <!-- Neon gradient arc -->
    <circle cx={CX} cy={CY} r={ORBIT}
      fill="none"
      stroke="url(#ag-arcGrad)"
      stroke-width="2"
      opacity="0.5"
    />

    <!-- Center logo -->
    <image
      href="/Antra_newlogo.png"
      x={CX - 130}
      y={CY - 130}
      width="260"
      height="260"
      filter="url(#ag-logoGlow)"
    />

    <!-- Service icons — sorted so spotlight renders on top -->
    {#each sorted as icon (icon.id)}
      {@const isSpot = icon.spotlight > 0.5}
      <g
        transform="translate({icon.x},{icon.y}) scale({icon.scale})"
        style="opacity:{icon.opacity}"
        filter={isSpot ? 'url(#ag-spotGlow)' : 'url(#ag-iconGlow)'}
      >
        <!-- Spotlight glow ring -->
        {#if isSpot}
          <circle cx="0" cy="0" r="20"
            fill="none"
            stroke={icon.color}
            stroke-width="2.5"
            opacity={icon.spotlight * 0.9}
          />
        {/if}

        <!-- Dark background circle with colored border -->
        <circle cx="0" cy="0" r="18"
          fill="rgba(10,15,15,0.85)"
          stroke={icon.color}
          stroke-width={isSpot ? 2 : 1}
          opacity={isSpot ? 1 : 0.7}
        />

        <!-- Icon SVG centered at 0,0 -->
        <svg x="-14" y="-14" width="28" height="28" viewBox="0 0 24 24" overflow="visible">
          {#if icon.id === 'spotify'}
            <circle cx="12" cy="12" r="12" fill="#1DB954"/>
            <path d="M17.9 10.9C14.7 9 9.35 8.8 6.3 9.75c-.5.15-1-.15-1.15-.6-.15-.5.15-1 .6-1.15 3.55-1.05 9.4-.85 13.1 1.35.45.25.6.85.35 1.3-.25.35-.85.5-1.3.25zm-.1 2.8c-.25.4-.75.5-1.15.25-2.7-1.65-6.8-2.15-9.95-1.15-.4.1-.85-.1-.95-.5-.1-.4.1-.85.5-.95 3.65-1.1 8.15-.55 11.25 1.35.4.25.5.75.3 1zm-1.3 2.7c-.2.35-.6.45-.95.25-2.35-1.45-5.3-1.75-8.8-.95-.35.1-.65-.15-.75-.45-.1-.35.15-.65.45-.75 3.8-.85 7.1-.5 9.7 1.1.35.15.45.6.35.8z" fill="white"/>

          {:else if icon.id === 'applemusic'}
            <circle cx="12" cy="12" r="12" fill="#fc3c44"/>
            <path d="M16.5 7.5l-6 1.5v6.25a2 2 0 1 0 1.5 1.94V11l4.5-1.13V7.5z" fill="white"/>

          {:else if icon.id === 'amazon'}
            <circle cx="12" cy="12" r="12" fill="#1A73E8"/>
            <text x="12" y="10" text-anchor="middle" fill="white" font-size="5" font-weight="bold" font-family="Arial">music</text>
            <path d="M6 13 Q12 17 18 13" stroke="#FF9900" stroke-width="1.5" fill="none" stroke-linecap="round"/>
            <path d="M16.5 12.5 L18 13 L17 14.3" stroke="#FF9900" stroke-width="1.5" fill="none" stroke-linecap="round" stroke-linejoin="round"/>

          {:else if icon.id === 'tidal'}
            <circle cx="12" cy="12" r="12" fill="white"/>
            <image href="/icons/tidal.webp" x="4" y="4" width="16" height="16" preserveAspectRatio="xMidYMid meet"/>

          {:else if icon.id === 'deezer'}
            <circle cx="12" cy="12" r="12" fill="#a020f0"/>
            <rect x="4.5" y="9"  width="2" height="6"  fill="white" rx="0.8"/>
            <rect x="8"   y="7"  width="2" height="10" fill="white" rx="0.8"/>
            <rect x="11.5" y="8" width="2" height="8"  fill="white" rx="0.8"/>
            <rect x="15"  y="6"  width="2" height="12" fill="white" rx="0.8"/>
            <rect x="18.5" y="9" width="2" height="6"  fill="white" rx="0.8"/>

          {:else if icon.id === 'link'}
            <circle cx="12" cy="12" r="12" fill="#1a3a8f"/>
            <path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71" stroke="white" stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round"/>
            <path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71" stroke="white" stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round"/>

          {:else if icon.id === 'download'}
            <circle cx="12" cy="12" r="12" fill="#0d7a7a"/>
            <path d="M12 5v9M8 10l4 4 4-4M5 18h14" stroke="white" stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round"/>

          {:else if icon.id === 'search'}
            <circle cx="12" cy="12" r="12" fill="#2a2a2a"/>
            <circle cx="10.5" cy="10.5" r="4" stroke="white" stroke-width="2" fill="none"/>
            <line x1="14" y1="14" x2="18" y2="18" stroke="white" stroke-width="2" stroke-linecap="round"/>
          {/if}
        </svg>
      </g>
    {/each}
  </svg>

  <!-- Spotlight service label -->
  <div style="margin-top:-18px;height:28px;display:flex;align-items:center;justify-content:center;">
    <span style="
      color:{activeService?.color || '#00d4d4'};
      font-family:'Courier New',monospace;
      font-size:13px;
      font-weight:bold;
      letter-spacing:0.18em;
      text-transform:uppercase;
      opacity:{labelOpacity};
      text-shadow:0 0 12px {activeService?.glowColor || 'rgba(0,212,212,0.6)'};
    ">{activeLabel}</span>
  </div>
</div>
