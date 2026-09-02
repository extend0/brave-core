// Copyright (c) 2026 The Brave Authors. All rights reserved.
// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this file,
// You can obtain one at https://mozilla.org/MPL/2.0/.

'use strict';

// SoundCloud's mobile web app only registers play/pause/seekforward/seekbackward
// Media Session handlers and never nexttrack/previoustrack. WebKit therefore
// advertises "skip 15s" to iOS instead of Next/Previous Track, so headphone and
// lock screen "next" presses seek instead of changing tracks. Register the
// missing handlers here and forward them to SoundCloud's own player.
window.__firefox__.includeOnce("SoundCloudMediaSessionScript", function($) {
  const host = window.location.hostname;
  if (host !== 'soundcloud.com' && !host.endsWith('.soundcloud.com')) {
    return;
  }
  if (window.top !== window) {
    return;
  }
  if (!navigator.mediaSession || typeof MediaSession === 'undefined') {
    return;
  }

  // SoundCloud's player reducer accepts initiated: "AUTO" | "KEY" | "UI".
  const INITIATED_BY_KEY = "KEY";
  const MAX_FIBER_NODES = 50000;

  // Latest handlers the site registered, keyed by action. Used to fall back to
  // the site's own seek behaviour if its player cannot be found.
  const siteHandlers = Object.create(null);

  // ---- Strategy 1: SoundCloud's player context via the React fiber tree ----

  const isPlayerContext = $(function(value) {
    return value !== null && typeof value === 'object' &&
      typeof value.playNext === 'function' && typeof value.playPrev === 'function';
  });

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

  // Bounded depth-first walk. Nothing is cached because fibers are
  // double-buffered and context values are recreated on render; this only runs
  // on a remote command press so the cost is negligible.
  const findPlayerContext = $(function() {
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
      if (props && isPlayerContext(props.value)) {
        return props.value;
      }
      // useContext consumers keep it in the dependencies chain.
      let dep = fiber.dependencies && fiber.dependencies.firstContext;
      while (dep) {
        if (isPlayerContext(dep.memoizedValue)) {
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

  // ---- Strategy 2: full-screen player buttons (prev, play/pause, next) ----

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

  // ---- Dispatch ----

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

  const originalSetActionHandler = MediaSession.prototype.setActionHandler;
  if (typeof originalSetActionHandler !== 'function') {
    return;
  }

  const installHandlers = $(function() {
    try {
      originalSetActionHandler.call(navigator.mediaSession, 'nexttrack', onNextTrack);
      originalSetActionHandler.call(navigator.mediaSession, 'previoustrack', onPreviousTrack);
    } catch (e) {
      // Media Session actions unsupported on this WebKit; nothing to do.
    }
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

  installHandlers();
});
