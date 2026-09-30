import React, { useEffect, useState } from "react";
import { fetchReviewQueue } from "./services/reviewQueueService";

const REFRESH_MS = 60_000;

export default function ReviewQueueNavButton({ userId, onClick }) {
  const [count, setCount] = useState(0);

  useEffect(() => {
    let cancelled = false;
    const load = () =>
      fetchReviewQueue(userId)
        .then((res) => {
          if (!cancelled && res.status === "success") setCount((res.data?.items || []).length);
        })
        .catch(() => {});
    load();
    const timer = setInterval(load, REFRESH_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [userId]);

  const label = count > 0 ? `Review queue, ${count} uncategorized item${count === 1 ? "" : "s"}` : "Review queue";

  return (
    <button onClick={onClick} className="nav-btn review-nav-btn" aria-label={label} title={label}>
      <i className="fa-solid fa-list-check" aria-hidden="true"></i>
      <span>Review</span>
      {count > 0 && (
        <span className="review-nav-badge" aria-hidden="true">
          {count > 99 ? "99+" : count}
        </span>
      )}
    </button>
  );
}
