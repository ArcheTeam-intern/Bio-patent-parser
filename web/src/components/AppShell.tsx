import { NavLink, Outlet } from "react-router-dom";

export function AppShell() {
  return (
    <>
      <header className="app-header">
        <div>
          <h1>生物 PDF 解析</h1>
          <div className="subtitle">序列、活性与原文证据</div>
        </div>
        <nav className="app-nav">
          <NavLink to="/jobs">任务</NavLink>
          <NavLink to="/reviews">审核队列</NavLink>
          <NavLink to="/compare">解析器对比</NavLink>
        </nav>
      </header>
      <main className="app-main">
        <Outlet />
      </main>
    </>
  );
}
