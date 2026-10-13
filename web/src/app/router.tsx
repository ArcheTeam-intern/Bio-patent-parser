import { Navigate, createBrowserRouter } from "react-router-dom";
import { AppShell } from "../components/AppShell";
import { JobsPage } from "../features/jobs/JobsPage";
import { WorkbenchPage } from "../features/workbench/WorkbenchPage";
import { ReviewQueuePage } from "../features/reviews/ReviewQueuePage";
import { ComparePage } from "../features/compare/ComparePage";

export const router = createBrowserRouter([
  {
    path: "/",
    element: <AppShell />,
    children: [
      { index: true, element: <Navigate to="/jobs" replace /> },
      { path: "jobs", element: <JobsPage /> },
      { path: "jobs/:jobId/workbench", element: <WorkbenchPage /> },
      { path: "reviews", element: <ReviewQueuePage /> },
      { path: "compare", element: <ComparePage /> },
    ],
  },
]);
