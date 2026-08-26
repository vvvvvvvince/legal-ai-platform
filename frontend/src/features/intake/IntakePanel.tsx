import type { ChangeEvent, FormEvent, RefObject } from "react";

type Props = {
  contractOverview: unknown;
  file: File | null;
  isLoading: boolean;
  isIntakeChatLoading: boolean;
  intakeChatDraft: string;
  intakeChatWarning: string | null;
  error: string | null;
  canStop: boolean;
  fileInputRef: RefObject<HTMLInputElement | null>;
  onDraftChange: (value: string) => void;
  onSend: (event?: FormEvent) => void;
  onStop: () => void;
};

export function IntakePanel({ contractOverview, file, isLoading, isIntakeChatLoading, intakeChatDraft, intakeChatWarning, error, canStop, fileInputRef, onDraftChange, onSend, onStop }: Props) {
  return (
    <div className="legal-chat-dock">
      {intakeChatWarning ? <p className="legal-chat-notice" role="status">{intakeChatWarning}</p> : null}
      {error ? <p className="error-message legal-chat-error">{error}</p> : null}
      <form className="legal-chat-composer" onSubmit={(event) => { if (contractOverview) { onSend(event); return; } event.preventDefault(); if (intakeChatDraft.trim()) onSend(); else if (!file) fileInputRef.current?.click(); }}>
        <textarea value={intakeChatDraft} maxLength={2000} disabled={isIntakeChatLoading || isLoading} onChange={(event: ChangeEvent<HTMLTextAreaElement>) => onDraftChange(event.target.value)} onKeyDown={(event) => { if (event.key !== "Enter" || event.shiftKey || event.nativeEvent.isComposing || !intakeChatDraft.trim()) return; event.preventDefault(); onSend(); }} placeholder={contractOverview ? "告诉 AI 您的立场、业务目标、顾虑，或直接查询相关法规…" : file ? "正在自动读取合同…" : "咨询法规、法条或合同问题；也可通过左侧上传文件"} />
        <div className="legal-chat-composer-actions"><div className="legal-chat-composer-left-actions"><button className="legal-chat-attach" type="button" disabled={isLoading || isIntakeChatLoading} onClick={() => fileInputRef.current?.click()}>上传文件</button><button className="legal-chat-stop" type="button" disabled={!canStop} onClick={onStop} title="终止当前输入或正在生成的回复">终止</button></div><button className="legal-chat-send" type="submit" title="Enter" aria-label="Enter" disabled={isLoading || isIntakeChatLoading || !intakeChatDraft.trim()}>{isIntakeChatLoading ? "…" : "Enter"}</button></div>
      </form>
      <div className="legal-chat-dock-footer"><span>支持 DOCX / PDF，最大 10MB · 合同内容仅用于本次审查</span></div>
    </div>
  );
}
