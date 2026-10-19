import { useCallback, useEffect, useRef, type RefObject } from 'react';

/** How close to the bottom (in px) still counts as "reading the latest". */
const NEAR_BOTTOM = 64;

/**
 * Keep the page scrolled to `endRef` whenever `content` changes, but only while the reader
 * is at the bottom: scrolling up to reread stops following. `follow()` resumes it (on send).
 */
export function useFollowBottom(endRef: RefObject<HTMLElement | null>, content: unknown): () => void {
  const followingRef = useRef(true);

  useEffect(() => {
    function onScroll() {
      const bottom = window.scrollY + window.innerHeight;
      followingRef.current = bottom >= document.documentElement.scrollHeight - NEAR_BOTTOM;
    }
    window.addEventListener('scroll', onScroll, { passive: true });
    return () => window.removeEventListener('scroll', onScroll);
  }, []);

  useEffect(() => {
    const end = endRef.current;
    // jsdom has no scrollIntoView; browsers do.
    if (followingRef.current && end !== null && typeof end.scrollIntoView === 'function') {
      end.scrollIntoView({ block: 'end' });
    }
  }, [endRef, content]);

  return useCallback(() => {
    followingRef.current = true;
  }, []);
}
