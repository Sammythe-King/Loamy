import React, { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { faListCheck } from "@fortawesome/free-solid-svg-icons";
import { fetchReviewQueue } from "./services/reviewQueueService";

const REFRESH_MS = 60_000;

// Sidebar link to the Review Queue with a live count of items still needing a
// category. `refreshKey` lets the chat page re-check the count after each reply,
// since the chat can resolve queue items on its own.
export default function ChatReviewLink({ userId, refreshKey }) {
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
  }, [userId, refreshKey]);

  const label = count > 0 ? `Review Queue, ${count} item${count === 1 ? "" : "s"} to review` : "Review Queue";

  return (
    <Link to="/review-queue" className="menu-item" aria-label={label}>
      <FontAwesomeIcon icon={faListCheck} />
      <span>Review Queue</span>
      {count > 0 && (
        <span className="menu-item-badge" aria-hidden="true">
          {count > 99 ? "99+" : count}
        </span>
      )}
    </Link>
  );
}
