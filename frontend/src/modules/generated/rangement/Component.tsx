import { useState, useEffect, useCallback, useRef } from "react";
import { FolderOpen, Search, Download, Trash2, Zap, Cpu } from "lucide-react";
import type { SharedModuleProps } from "../../registry";
import { usePersistentState } from "../../../usePersistentState";
import { Button, Card, Badge, Input, Tabs, Select } from "../../../components/ui";
import { API, apiFetch } from '../../../api'

// ── Types (identiques à ceux du backend) ──────────────────────────────────────

interface FileInfo {
  name: string;
  size: number;
  relativePath: string;
  extension: string;
}

interface AnalyzeResponse {
  plan: { [folder: string]: string[] };
  summary: { [relPath: string]: string };
  duplicates: { [keepRel: string]: string[] };
  powershell_script: string;
}

interface HistoryEntry {
  timestamp: string;
  files_count: number;
  theme: string;
  plan: { [folder: string]: string[] };
  powershell_script?: string;
  model_used?: string;
}

interface AvailableModel {
  id: string;
  nom: string;
  provider: string;
  type: string;       // "local" | "npu"
  gratuit: boolean;
  description: string;
  available: boolean;
}

// ── Composant ─────────────────────────────────────────────────────────────────

export default function RangementModule(_props: SharedModuleProps) {
  // ── State persistant (survit à F5 / reload atelier) ─────────────────────────
  const [theme, setTheme] = usePersistentState<string>("rangement.theme", "");
  const [activeTab, setActiveTab] = usePersistentState<string>(
    "rangement.activeTab",
    "analysis"
  );
  const [selectedModel, setSelectedModel] = usePersistentState<string>(
    "rangement.model",
    ""
  );
  const [streamMode, setStreamMode] = usePersistentState<boolean>(
    "rangement.streamMode",
    true
  );

  // ── State éphémère ──────────────────────────────────────────────────────────
  const [fileList, setFileList] = useState<FileInfo[]>([]);
  const [result, setResult] = useState<AnalyzeResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [folderPath, setFolderPath] = useState("");
  const [history, setHistory] = useState<HistoryEntry[]>([]);
  const [models, setModels] = useState<AvailableModel[]>([]);

  // Streaming state
  const [streamTokens, setStreamTokens] = useState<string>("");
  const [streamStatus, setStreamStatus] = useState<string>("");
  const [isStreaming, setIsStreaming] = useState(false);

  const abortRef = useRef<AbortController | null>(null);

  // ── Chargement des modèles disponibles ──────────────────────────────────────

  const fetchModels = useCallback(async () => {
    try {
      const res = await apiFetch(`${API}/rangement/models`);
      if (res.ok) {
        const data = await res.json();
        setModels(data.models ?? []);
      }
    } catch {
      // silencieux
    }
  }, []);

  useEffect(() => {
    fetchModels();
  }, [fetchModels]);

  // ── Chargement de l'historique ──────────────────────────────────────────────

  const fetchHistory = useCallback(async () => {
    try {
      const res = await apiFetch(`${API}/rangement/history`);
      if (res.ok) {
        const data = await res.json();
        setHistory(data.history ?? []);
      }
    } catch {
      // silencieux
    }
  }, []);

  useEffect(() => {
    fetchHistory();
  }, [fetchHistory]);

  useEffect(() => {
    if (activeTab === "history") {
      fetchHistory();
    }
  }, [activeTab, fetchHistory]);

  // ── Sélection de dossier (File System Access API) ───────────────────────────

  async function readFolder(
    dirHandle: FileSystemDirectoryHandle,
    relativeParent: string,
    files: FileInfo[]
  ): Promise<void> {
    for await (const [name, handle] of (dirHandle as any).entries()) {
      const childRel = relativeParent ? `${relativeParent}/${name}` : name;
      if (handle.kind === "file") {
        const file = await handle.getFile();
        const dot = name.lastIndexOf(".");
        const ext = dot !== -1 ? `.${name.slice(dot + 1)}` : "";
        files.push({
          name,
          size: file.size,
          relativePath: childRel,
          extension: ext.toLowerCase(),
        });
      } else if (handle.kind === "directory") {
        await readFolder(handle, childRel, files);
      }
    }
  }

  const handleSelectFolder = async () => {
    setError(null);
    try {
      const dirHandle = await (window as any).showDirectoryPicker();
      setFolderPath(dirHandle.name || "");
      const collected: FileInfo[] = [];
      await readFolder(dirHandle, "", collected);
      setFileList(collected);
      setResult(null);
      setStreamTokens("");
      setStreamStatus("");
    } catch (err: any) {
      if (err?.name !== "AbortError") {
        setError("Impossible d'ouvrir le dossier. Vérifiez les permissions.");
        console.error("Folder selection error", err);
      }
    }
  };

  // ── Analyse (non-streaming) ─────────────────────────────────────────────────

  const handleAnalyze = async () => {
    if (fileList.length === 0) return;
    if (loading) return;

    setError(null);
    setResult(null);
    setStreamTokens("");
    setStreamStatus("");
    setLoading(true);

    try {
      const res = await apiFetch(`${API}/rangement/analyze`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          files: fileList,
          theme,
          model: selectedModel || null,
        }),
      });

      if (!res.ok) {
        const text = await res.text();
        throw new Error(text || `Erreur HTTP ${res.status}`);
      }

      const data: AnalyzeResponse = await res.json();
      setResult(data);
      fetchHistory();
    } catch (err: any) {
      console.error("Analyze error", err);
      setError(err?.message || "Erreur lors de l'analyse.");
      setResult(null);
    } finally {
      setLoading(false);
    }
  };

  // ── Analyse streaming (SSE) ─────────────────────────────────────────────────

  const handleAnalyzeStream = async () => {
    if (fileList.length === 0) return;
    if (loading || isStreaming) return;

    // Annule un éventuel stream précédent
    if (abortRef.current) {
      abortRef.current.abort();
    }

    setError(null);
    setResult(null);
    setStreamTokens("");
    setStreamStatus("Connexion au serveur…");
    setIsStreaming(true);
    setLoading(true);

    const abort = new AbortController();
    abortRef.current = abort;

    try {
      const res = await apiFetch(`${API}/rangement/analyze-stream`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          files: fileList,
          theme,
          model: selectedModel || null,
        }),
        signal: abort.signal,
      });

      if (!res.ok) {
        const text = await res.text();
        throw new Error(text || `Erreur HTTP ${res.status}`);
      }

      const reader = res.body?.getReader();
      if (!reader) throw new Error("Pas de corps de réponse (streaming non supporté)");

      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });

        // Parse les événements SSE complets dans le buffer
        const lines = buffer.split("\n");
        buffer = lines.pop() || ""; // garde le dernier fragment incomplet

        let currentEvent = "";
        for (const line of lines) {
          if (line.startsWith("event: ")) {
            currentEvent = line.slice(7).trim();
          } else if (line.startsWith("data: ")) {
            const raw = line.slice(6);
            try {
              const payload = JSON.parse(raw);
              handleSSEEvent(currentEvent, payload);
            } catch {
              // ligne non-JSON, on ignore
            }
            currentEvent = "";
          }
        }
      }
    } catch (err: any) {
      if (err?.name === "AbortError") {
        // annulation volontaire, rien à faire
      } else {
        console.error("Stream error", err);
        setError(err?.message || "Erreur lors du streaming.");
      }
    } finally {
      setIsStreaming(false);
      setLoading(false);
      abortRef.current = null;
    }
  };

  /** Traite un événement SSE reçu du backend. */
  function handleSSEEvent(event: string, payload: any) {
    switch (event) {
      case "status":
        setStreamStatus(payload.message || "");
        break;
      case "token":
        setStreamTokens((prev) => prev + (payload.text || ""));
        break;
      case "result":
        setResult(payload as AnalyzeResponse);
        setStreamStatus("Analyse terminée.");
        fetchHistory();
        break;
      case "error":
        setError(payload.message || "Erreur inconnue");
        setStreamStatus("");
        break;
      case "done":
        // flux terminé
        break;
    }
  }

  /** Annule le streaming en cours. */
  const cancelStream = () => {
    if (abortRef.current) {
      abortRef.current.abort();
      abortRef.current = null;
    }
    setIsStreaming(false);
    setLoading(false);
    setStreamStatus("Annulé.");
  };

  // ── Lancer l'analyse (streaming ou classique selon le toggle) ───────────────

  const handleLaunch = () => {
    if (streamMode) {
      handleAnalyzeStream();
    } else {
      handleAnalyze();
    }
  };

  // ── Téléchargement du script PowerShell ─────────────────────────────────────

  const downloadScript = () => {
    if (!result?.powershell_script) return;
    _downloadPs1(result.powershell_script);
  };

  const _downloadPs1 = (script: string, filename: string = "rangement.ps1") => {
    if (!script) return;
    const blob = new Blob([script], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
  };

  // ── Total des doublons ──────────────────────────────────────────────────────

  const totalDuplicates = result
    ? Object.values(result.duplicates).reduce((sum, dups) => sum + dups.length, 0)
    : 0;

  // ── Groupement des modèles par type ─────────────────────────────────────────

  const ollamaModels = models.filter((m) => m.provider === "ollama");
  const npuModels = models.filter((m) => m.provider === "flm");

  // ── Rendu ───────────────────────────────────────────────────────────────────

  return (
    <main className="flex flex-col flex-1 overflow-y-auto px-8 py-8 gap-4">
      <h1 className="text-xl font-semibold text-primary flex items-center gap-2">
        <FolderOpen size={18} className="text-accent" /> Rangement intelligent
      </h1>
      <p className="text-sm text-secondary max-w-2xl leading-relaxed">
        Sélectionnez un dossier, choisissez un modèle et un thème optionnel, puis
        lancez l'analyse. Le LLM classe vos fichiers, résume leurs noms, détecte
        les doublons et génère un script PowerShell. Le mode streaming vous
        permet de voir la réponse en temps réel.
      </p>

      <Tabs
        tabs={[
          { id: "analysis", label: "🔍 Analyse" },
          { id: "history", label: "📋 Historique" },
        ]}
        active={activeTab}
        onChange={setActiveTab}
      />

      {/* ── Onglet Analyse ── */}
      {activeTab === "analysis" && (
        <Card className="max-w-2xl space-y-4">
          {/* Sélection du dossier */}
          <div className="flex items-center gap-3 flex-wrap">
            <Button
              variant="primary"
              size="sm"
              icon={<FolderOpen size={14} />}
              onClick={handleSelectFolder}
            >
              Choisir un dossier
            </Button>
            {fileList.length > 0 && (
              <Badge variant="success">
                {fileList.length} fichier{fileList.length > 1 ? "s" : ""} trouvé{fileList.length > 1 ? "s" : ""}
              </Badge>
            )}
            {folderPath && (
              <span className="text-xs text-muted font-mono">{folderPath}/</span>
            )}
          </div>

          {/* Sélecteur de modèle */}
          {models.length > 0 && (
            <div>
              <label
                htmlFor="rangement-model"
                className="text-xs text-muted uppercase tracking-wide block mb-1"
              >
                Modèle LLM
              </label>
              <Select
                id="rangement-model"
                value={selectedModel}
                onChange={(e) => setSelectedModel(e.target.value)}
                className="w-full"
              >
                <option value="">Modèle par défaut</option>

                {ollamaModels.length > 0 && (
                  <optgroup label="── Local (Ollama) ──">
                    {ollamaModels.map((m) => (
                      <option key={m.id} value={m.id}>
                        🖥️ {m.nom}
                      </option>
                    ))}
                  </optgroup>
                )}

                {npuModels.length > 0 && (
                  <optgroup label="── Local NPU (FLM) ──">
                    {npuModels.map((m) => (
                      <option
                        key={m.id}
                        value={m.id}
                        disabled={!m.available}
                      >
                        {m.available ? "⚡" : "❌"} {m.nom}
                        {!m.available ? " (non installé)" : ""}
                        {m.description ? ` — ${m.description}` : ""}
                      </option>
                    ))}
                  </optgroup>
                )}
              </Select>
              {selectedModel && (
                <p className="text-xs text-muted mt-1">
                  {(() => {
                    const m = models.find((x) => x.id === selectedModel);
                    if (!m) return "";
                    const badge =
                      m.type === "npu"
                        ? "NPU (FLM)"
                        : m.provider === "ollama"
                          ? "Local CPU/GPU"
                          : m.provider;
                    return `${badge} — ${m.description || m.nom}`;
                  })()}
                </p>
              )}
            </div>
          )}

          {/* Thème */}
          <div>
            <label
              htmlFor="rangement-theme"
              className="text-xs text-muted uppercase tracking-wide block mb-1"
            >
              Thème souhaité (optionnel)
            </label>
            <Input
              id="rangement-theme"
              type="text"
              placeholder="ex: photos de vacances, documents administratifs, code source..."
              value={theme}
              onChange={(e) => setTheme(e.target.value)}
              className="w-full"
            />
          </div>

          {/* Options de streaming + bouton */}
          <div className="flex items-center gap-3 flex-wrap">
            <Button
              variant="primary"
              icon={isStreaming ? undefined : <Search size={14} />}
              onClick={handleLaunch}
              disabled={loading || fileList.length === 0}
            >
              {isStreaming
                ? "Streaming…"
                : loading
                  ? "Analyse en cours…"
                  : "Analyser"}
            </Button>

            {/* Toggle streaming */}
            <label className="flex items-center gap-1.5 text-xs text-secondary cursor-pointer select-none">
              <input
                type="checkbox"
                checked={streamMode}
                onChange={(e) => setStreamMode(e.target.checked)}
                className="accent-accent"
              />
              <Zap size={13} className="text-accent" />
              Streaming temps réel
            </label>

            {isStreaming && (
              <Button variant="secondary" size="sm" onClick={cancelStream}>
                Annuler
              </Button>
            )}
          </div>

          {/* Erreur */}
          {error && (
            <p className="text-xs text-error bg-error/5 border border-error/20 rounded-sm px-3 py-2">
              {error}
            </p>
          )}

          {/* ── Affichage temps réel du streaming ── */}
          {(isStreaming || streamTokens) && (
            <div className="border border-line rounded-sm overflow-hidden">
              <div className="flex items-center gap-2 px-3 py-1.5 bg-elevated border-b border-line">
                <Cpu size={12} className="text-accent animate-pulse" />
                <span className="text-xs font-semibold text-primary">
                  Réponse du LLM en direct
                </span>
                {streamStatus && (
                  <span className="text-xs text-muted ml-auto">
                    {streamStatus}
                  </span>
                )}
              </div>
              <pre className="text-xs font-mono text-secondary p-3 max-h-64 overflow-y-auto whitespace-pre-wrap bg-[#0d1117]">
                {streamTokens || (
                  <span className="text-muted italic">
                    En attente des premiers tokens…
                  </span>
                )}
              </pre>
            </div>
          )}

          {/* Résultats */}
          {result && (
            <div className="space-y-4 pt-2 border-t border-line">
              {/* Plan de rangement */}
              <div>
                <h3 className="text-sm font-semibold text-primary mb-2">
                  Plan de rangement
                </h3>
                <div className="space-y-1">
                  {Object.entries(result.plan).map(([folder, files]) => (
                    <div key={folder} className="text-xs">
                      <span className="font-semibold text-accent">{folder}</span>{" "}
                      <span className="text-muted">
                        ({files.length} fichier{files.length > 1 ? "s" : ""})
                      </span>
                      <span className="text-secondary">
                        {" "}— {files.slice(0, 5).join(", ")}
                        {files.length > 5 && ` … et ${files.length - 5} autre(s)`}
                      </span>
                    </div>
                  ))}
                </div>
              </div>

              {/* Résumés */}
              <div>
                <h3 className="text-sm font-semibold text-primary mb-2">
                  Résumés
                </h3>
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-1 max-h-48 overflow-y-auto">
                  {Object.entries(result.summary).map(([rel, summ]) => (
                    <div
                      key={rel}
                      className="text-xs text-secondary flex justify-between gap-2"
                    >
                      <span className="text-muted truncate">{rel}</span>
                      <span className="text-primary shrink-0">{summ}</span>
                    </div>
                  ))}
                </div>
              </div>

              {/* Doublons */}
              {Object.keys(result.duplicates).length > 0 && (
                <div>
                  <h3 className="text-sm font-semibold text-primary mb-2 flex items-center gap-2">
                    <Trash2 size={13} className="text-warning" />
                    Doublons détectés
                    <Badge variant="warning">{totalDuplicates}</Badge>
                  </h3>
                  <div className="space-y-1 max-h-40 overflow-y-auto">
                    {Object.entries(result.duplicates).map(([keep, dups]) => (
                      <div key={keep} className="text-xs">
                        <span className="text-success font-mono">{keep}</span>{" "}
                        <span className="text-muted">(conservé)</span>
                        <span className="text-error">
                          {" "}← supprimer {dups.join(", ")}
                        </span>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* Script PowerShell */}
              <div>
                <h3 className="text-sm font-semibold text-primary mb-2">
                  Script PowerShell
                </h3>
                <pre className="text-xs font-mono text-secondary bg-elevated border border-line rounded-sm p-3 max-h-60 overflow-auto whitespace-pre-wrap">
                  {result.powershell_script}
                </pre>
                <Button
                  variant="secondary"
                  size="sm"
                  icon={<Download size={13} />}
                  onClick={downloadScript}
                  className="mt-2"
                >
                  Télécharger le script
                </Button>
              </div>
            </div>
          )}
        </Card>
      )}

      {/* ── Onglet Historique ── */}
      {activeTab === "history" && (
        <Card className="max-w-2xl space-y-3">
          <h2 className="text-sm font-semibold text-primary">
            Historique des analyses
          </h2>
          {history.length === 0 ? (
            <p className="text-xs text-muted">
              Aucune analyse enregistrée pour le moment.
            </p>
          ) : (
            <div className="space-y-1 max-h-96 overflow-y-auto">
              {history
                .slice()
                .reverse()
                .map((entry, idx) => (
                  <div
                    key={idx}
                    className="text-xs text-secondary flex items-center gap-3 py-1 border-b border-line last:border-0"
                  >
                    <span className="text-muted shrink-0 w-36">
                      {new Date(entry.timestamp).toLocaleString()}
                    </span>
                    <Badge variant="neutral">{entry.files_count} fichier(s)</Badge>
                    {entry.model_used && entry.model_used !== "fallback" && (
                      <Badge variant="success">{entry.model_used}</Badge>
                    )}
                    {entry.model_used === "fallback" && (
                      <Badge variant="warning">heuristique</Badge>
                    )}
                    {entry.theme && (
                      <span className="text-muted truncate">
                        thème : {entry.theme}
                      </span>
                    )}
                    {entry.powershell_script && (
                      <button
                        title="Télécharger le script PowerShell de cette analyse"
                        className="ml-auto shrink-0 text-muted hover:text-accent transition-colors p-1 rounded-sm hover:bg-elevated"
                        onClick={() =>
                          _downloadPs1(
                            entry.powershell_script!,
                            `rangement_${entry.timestamp.slice(0, 10)}.ps1`
                          )
                        }
                      >
                        <Download size={13} />
                      </button>
                    )}
                  </div>
                ))}
            </div>
          )}
        </Card>
      )}
    </main>
  );
}
