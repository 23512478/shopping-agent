import DeadLetterPage from '../components/DeadLetterPage'

// 管理端页面：死信队列（节点 ID、错误栈、输入快照）只给开发者排查用，
// 不进任何 C 端导航，C 端遇到内部失败只显示友好提示。
export default function AdminDeadLetter() {
  return (
    <div className="page admin">
      <div className="admin-banner">
        <b>开发者管理端</b>
        <span>失败任务（死信队列）仅供运维排查，不对 C 端暴露</span>
      </div>
      <DeadLetterPage />
    </div>
  )
}