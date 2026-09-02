// Copyright (c) 2026 The Brave Authors. All rights reserved.
// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this file,
// You can obtain one at https://mozilla.org/MPL/2.0/.

'use strict';

// Fixes for SoundCloud's mobile web app (the Next.js app served to iPhones):
//
// 1. Media Session: the mobile app only registers play/pause/seekforward/
//    seekbackward handlers and never nexttrack/previoustrack, so WebKit
//    advertises "skip 15s" to iOS instead of Next/Previous Track and headphone
//    or lock screen "next" presses seek instead of changing tracks. Register the
//    missing handlers and forward them to SoundCloud's own player.
//
// 2. Ordered playback: playlist pages hard-code a "Shuffle play" button and
//    never render the ordinary Play button that track pages get. Add a Play
//    button next to it that starts the playlist from the first track in order,
//    using the same player API the shuffle button uses.
window.__firefox__.includeOnce("SoundCloudScript", function($) {
  const host = window.location.hostname;
  if (host !== 'soundcloud.com' && !host.endsWith('.soundcloud.com')) {
    return;
  }
  if (window.top !== window) {
    return;
  }

  // SoundCloud's player reducer accepts initiated: "AUTO" | "KEY" | "UI".
  const INITIATED_BY_KEY = "KEY";
  const INITIATED_BY_UI = "UI";
  const MAX_FIBER_NODES = 50000;

  // ---- React internals ----

  const rootFiber = $(function() {
    const container = document.getElementById('__next') || document.body;
    if (!container) {
      return null;
    }
    // React 18 createRoot: __reactContainer$<random> on the container element.
    for (const key of Object.keys(container)) {
      if (key.startsWith('__reactContainer$')) {
        return container[key];
      }
    }
    // Legacy ReactDOM.render fallback.
    const legacy = container._reactRootContainer;
    if (legacy && legacy._internalRoot && legacy._internalRoot.current) {
      return legacy._internalRoot.current;
    }
    return null;
  });

  const fiberForNode = $(function(node) {
    for (const key of Object.keys(node)) {
      if (key.startsWith('__reactFiber$')) {
        return node[key];
      }
    }
    return null;
  });

  // Bounded depth-first walk for a context value matching `predicate`.
  // Nothing is cached because fibers are double-buffered and context values
  // are recreated on render; this only runs on user input so the cost is
  // negligible.
  const findContextValue = $(function(predicate) {
    const root = rootFiber();
    if (!root) {
      return null;
    }
    const stack = [root];
    let visited = 0;
    while (stack.length > 0 && visited++ < MAX_FIBER_NODES) {
      const fiber = stack.pop();
      // Context.Provider fibers keep the value in memoizedProps.value.
      const props = fiber.memoizedProps;
      if (props && predicate(props.value)) {
        return props.value;
      }
      // useContext consumers keep it in the dependencies chain.
      let dep = fiber.dependencies && fiber.dependencies.firstContext;
      while (dep) {
        if (predicate(dep.memoizedValue)) {
          return dep.memoizedValue;
        }
        dep = dep.next;
      }
      if (fiber.sibling) {
        stack.push(fiber.sibling);
      }
      if (fiber.child) {
        stack.push(fiber.child);
      }
    }
    return null;
  });

  const isPlayerContext = $(function(value) {
    return value !== null && typeof value === 'object' &&
      typeof value.playNext === 'function' && typeof value.playPrev === 'function';
  });

  // SoundCloud's player actions (playNext, playPrev, playSource, ...).
  const findPlayerContext = $(function() {
    return findContextValue(isPlayerContext);
  });

  const isReduxProviderContext = $(function(value) {
    return value !== null && typeof value === 'object' &&
      value.store !== null && typeof value.store === 'object' &&
      typeof value.store.getState === 'function';
  });

  // SoundCloud's redux store, via the react-redux Provider context.
  const findStore = $(function() {
    const context = findContextValue(isReduxProviderContext);
    return context ? context.store : null;
  });

  // ================= 1. Media Session next/previous track =================

  const installMediaSessionHandlers = $(function() {
    if (!navigator.mediaSession || typeof MediaSession === 'undefined') {
      return;
    }
    const originalSetActionHandler = MediaSession.prototype.setActionHandler;
    if (typeof originalSetActionHandler !== 'function') {
      return;
    }

    // Latest handlers the site registered, keyed by action. Used to fall back
    // to the site's own seek behaviour if its player cannot be found.
    const siteHandlers = Object.create(null);

    // Full-screen player buttons are (prev, play/pause, next).
    const fullPlayerButton = $(function(direction) {
      const controls = document.querySelector('[data-testid="fullplayer-controls"]');
      if (!controls) {
        return null;
      }
      const buttons = controls.querySelectorAll('button');
      if (buttons.length < 3) {
        return null;
      }
      const button = direction > 0 ? buttons[buttons.length - 1] : buttons[0];
      return button.disabled ? null : button;
    });

    const skipTrack = $(function(direction) {
      try {
        const player = findPlayerContext();
        if (player) {
          if (direction > 0) {
            player.playNext(INITIATED_BY_KEY);
          } else {
            player.playPrev(INITIATED_BY_KEY);
          }
          return;
        }
      } catch (e) {
        // Fall through to the next strategy.
      }

      try {
        const button = fullPlayerButton(direction);
        if (button) {
          button.click();
          return;
        }
      } catch (e) {
        // Fall through to the next strategy.
      }

      // Last resort: behave like stock SoundCloud (seek), or do nothing.
      try {
        const action = direction > 0 ? 'seekforward' : 'seekbackward';
        const fallback = siteHandlers[action];
        if (typeof fallback === 'function') {
          fallback({ action: action });
        }
      } catch (e) {
        // Never break the page.
      }
    });

    const onNextTrack = $(function() {
      skipTrack(1);
    });

    const onPreviousTrack = $(function() {
      skipTrack(-1);
    });

    // Observe the site's own registrations so we can keep its seek handlers as
    // fallbacks and keep our track handlers in place if the site ever clears
    // them. If the site registers a real nexttrack handler itself, it wins.
    const wrappedSetActionHandler = $(function(action, handler) {
      try {
        siteHandlers[action] = typeof handler === 'function' ? handler : null;
        if ((action === 'nexttrack' || action === 'previoustrack') && handler == null) {
          return originalSetActionHandler.call(
            this, action, action === 'nexttrack' ? onNextTrack : onPreviousTrack);
        }
      } catch (e) {
        // Fall through to the native implementation.
      }
      return originalSetActionHandler.call(this, action, handler);
    });

    try {
      Object.defineProperty(MediaSession.prototype, 'setActionHandler', {
        enumerable: false,
        configurable: true,
        writable: true,
        value: wrappedSetActionHandler
      });
    } catch (e) {
      // Wrapping is best effort; the handlers below still fix the bug.
    }

    try {
      originalSetActionHandler.call(navigator.mediaSession, 'nexttrack', onNextTrack);
      originalSetActionHandler.call(navigator.mediaSession, 'previoustrack', onPreviousTrack);
    } catch (e) {
      // Media Session actions unsupported on this WebKit; nothing to do.
    }
  });

  // ================= 2. Ordered "Play" button on playlist pages =================

  const installOrderedPlayButton = $(function() {
    if (typeof MutationObserver === 'undefined' || !document.createElementNS) {
      return;
    }

    // `playContext.type` / `sources[].entity` value for playlists.
    const PLAYLIST_ENTITY = 'playlists';
    const SYSTEM_PLAYLIST_ENTITY = 'systemPlaylists';
    const ORDERED_PLAY_ATTR = 'data-brave-ordered-play';
    const SHUFFLE_ICON_SELECTOR = 'svg[data-testid="shuffle-button"]';
    const SVG_NS = 'http://www.w3.org/2000/svg';
    const MAX_HYDRATION_RETRIES = 20;
    const HYDRATION_RETRY_MS = 250;

    const newId = $(function() {
      if (window.crypto && typeof window.crypto.randomUUID === 'function') {
        return window.crypto.randomUUID();
      }
      return String(Date.now()) + '-' + Math.random().toString(16).slice(2);
    });

    const isPlaylistContext = $(function(playContext) {
      return playContext !== null && typeof playContext === 'object' &&
        typeof playContext.urn === 'string' &&
        (playContext.type === PLAYLIST_ENTITY || playContext.type === SYSTEM_PLAYLIST_ENTITY);
    });

    // The shuffle button component receives { sources, playContext, isDefault }.
    // Walk up from the <button> fiber to it.
    const shufflePlayProps = $(function(shuffleButton) {
      let fiber = fiberForNode(shuffleButton);
      let hops = 0;
      while (fiber && hops++ < 20) {
        const props = fiber.memoizedProps;
        if (props && Array.isArray(props.sources) && isPlaylistContext(props.playContext)) {
          return { sources: props.sources, playContext: props.playContext };
        }
        fiber = fiber.return;
      }
      return null;
    });

    const trackUrnsFromEntities = $(function(entities, playContext) {
      if (!entities) {
        return null;
      }
      const bucket = entities[playContext.type];
      const wrapper = bucket && bucket[playContext.urn];
      const data = wrapper && wrapper.data;
      if (!data) {
        return null;
      }
      // SoundCloud's own queue builder reads `collection`; `tracks` mirrors it.
      const list = Array.isArray(data.collection) ? data.collection : data.tracks;
      if (!Array.isArray(list)) {
        return null;
      }
      const urns = list.filter(function(urn) { return typeof urn === 'string'; });
      return urns.length > 0 ? urns : null;
    });

    // Track URNs in playlist order, from the live store or (on a fresh load)
    // the server-rendered store snapshot. Both are keyed by the playlist URN so
    // a stale snapshot can never yield another playlist's tracks.
    const playlistTrackUrns = $(function(playContext) {
      try {
        const store = findStore();
        if (store) {
          const state = store.getState();
          const urns = trackUrnsFromEntities(state && state.entities, playContext);
          if (urns) {
            return urns;
          }
        }
      } catch (e) {
        // Fall through to the snapshot.
      }
      try {
        const nextData = window.__NEXT_DATA__;
        const pageProps = nextData && nextData.props && nextData.props.pageProps;
        const snapshot = pageProps && pageProps.initialStoreState;
        return trackUrnsFromEntities(snapshot && snapshot.entities, playContext);
      } catch (e) {
        return null;
      }
    });

    // Mirrors the shuffle button's click handler with shuffling disabled:
    // playSource(sources, playContext, tracks, initiated, trackUrn, fetchMore,
    // shuffling). Calling the player directly also skips the "open in app"
    // interstitial the site wraps its own buttons in.
    const playInOrder = $(function(shuffleButton) {
      const player = findPlayerContext();
      if (!player || typeof player.playSource !== 'function') {
        return false;
      }
      const props = shufflePlayProps(shuffleButton);
      if (!props) {
        return false;
      }
      const urns = playlistTrackUrns(props.playContext);
      if (!urns) {
        return false;
      }
      const tracks = urns.map(function(urn) {
        return {
          urn: urn,
          sources: props.sources,
          playContext: props.playContext,
          playQueueSourceId: newId()
        };
      });
      player.playSource(props.sources, props.playContext, tracks, INITIATED_BY_UI,
                        urns[0], false, false);
      return true;
    });

    const onOrderedPlay = $(function(shuffleButton) {
      try {
        if (playInOrder(shuffleButton)) {
          return;
        }
      } catch (e) {
        // Fall through.
      }
      // Fallback: let the site start playback its own way, then restore
      // playlist order from the current track onward if the player is
      // reachable. Both dispatches land in the same React batch.
      try {
        shuffleButton.click();
        const player = findPlayerContext();
        if (player && typeof player.toggleShuffle === 'function') {
          player.toggleShuffle(false);
        }
      } catch (e) {
        // Never break the page.
      }
    });

    const createOrderedPlayButton = $(function(shuffleButton) {
      const button = document.createElement('button');
      button.type = 'button';
      button.setAttribute(ORDERED_PLAY_ATTR, '');
      button.setAttribute('aria-label', 'Play');
      button.className = shuffleButton.className;
      button.style.marginRight = '8px';

      const icon = document.createElementNS(SVG_NS, 'svg');
      icon.setAttribute('viewBox', '0 0 32 32');
      icon.setAttribute('aria-hidden', 'true');
      const shuffleIcon = shuffleButton.querySelector('svg');
      if (shuffleIcon && shuffleIcon.getAttribute('class')) {
        // Reuse the shuffle icon's class so both icons share size and color.
        icon.setAttribute('class', shuffleIcon.getAttribute('class'));
      }
      const path = document.createElementNS(SVG_NS, 'path');
      path.setAttribute('d', 'M10 6v20l17-10z');
      path.setAttribute('fill', 'currentColor');
      icon.appendChild(path);
      button.appendChild(icon);

      button.addEventListener('click', $(function(event) {
        event.preventDefault();
        event.stopPropagation();
        onOrderedPlay(shuffleButton);
      }));
      return button;
    });

    // Returns true when the page has a shuffle button React has not hydrated
    // yet, so the caller should retry.
    const addButtons = $(function() {
      let needsRetry = false;
      const icons = document.querySelectorAll(SHUFFLE_ICON_SELECTOR);
      for (const icon of icons) {
        const shuffleButton = icon.closest('button');
        const parent = shuffleButton && shuffleButton.parentNode;
        if (!parent) {
          continue;
        }
        if (shuffleButton.previousElementSibling &&
            shuffleButton.previousElementSibling.hasAttribute(ORDERED_PLAY_ATTR)) {
          continue;
        }
        if (!fiberForNode(shuffleButton)) {
          needsRetry = true;
          continue;
        }
        // Only playlists: the same component also renders "shuffle all" on
        // other pages, whose queues we cannot rebuild.
        if (!shufflePlayProps(shuffleButton)) {
          continue;
        }
        parent.insertBefore(createOrderedPlayButton(shuffleButton), shuffleButton);
      }
      return needsRetry;
    });

    let scanScheduled = false;
    let retries = 0;
    let retryTimer = 0;

    const scan = $(function() {
      scanScheduled = false;
      let needsRetry = false;
      try {
        needsRetry = addButtons();
      } catch (e) {
        // Never break the page.
      }
      if (needsRetry && retries < MAX_HYDRATION_RETRIES) {
        retries++;
        clearTimeout(retryTimer);
        retryTimer = setTimeout(scan, HYDRATION_RETRY_MS);
      } else if (!needsRetry) {
        retries = 0;
      }
    });

    const scheduleScan = $(function() {
      if (scanScheduled) {
        return;
      }
      scanScheduled = true;
      requestAnimationFrame(scan);
    });

    // Injected at document start; the header arrives with the body and is
    // re-rendered on every client-side navigation, so watch the whole tree.
    const observer = new MutationObserver($(function() {
      scheduleScan();
    }));
    observer.observe(document, { childList: true, subtree: true });
    scheduleScan();
  });

  installMediaSessionHandlers();
  installOrderedPlayButton();
});
