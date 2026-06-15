import React, { useState, useEffect } from "react";

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
  powershellScript: string;
  error?: string;
}

const Component: React.FC = () => {
  const [theme, setTheme] = useState("");
  const [fileList, setFileList] = useState<FileInfo[]>([]);
  const [result, setResult] = useState<AnalyzeResponse | null>(null);
  const [loading, setLoading] = useState(false);

  const [models, setModels] = useState<string[]>([]);
  const [selectedModel, setSelectedModel] = useState<string>("");
  const [streaming, setStreaming] = useState(false);
  const [liveLog, setLiveLog] = useState<string[]>([]);

  useEffect(() => {
    fetch("/models")
      .then((r) => r.json())
      .then((data) => setModels(data.models ?? []))
      .catch(() => {});
  }, []);

  async function readFolder(
    dirHandle: FileSystemDirectoryHandle,
    relativeParent: string,
    files: FileInfo[]
  ): Promise<void> {
    for await (const [name, handle] of (dirHandle as any).entries()) {
      const childRel = relativeParent ? `${relativeParent}/${name}` : name;
      if (handle.kind === "file") {
        const file = await handle.getFile();
        const ext =
          name.lastIndexOf(".") !== -1 ? `.${name.split(".").pop()}` : "";
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
    try {
      const dirHandle = await (window as any).showDirectoryPicker();
      const collected: FileInfo[] = [];
      await readFolder(dirHandle, "", collected);
      setFileList(collected);
      setResult(null);
      setLiveLog([]);
    } catch (err: any) {
      console.error("Folder selection cancelled or error", err);
    }
  };

  const handleAnalyze = async () => {
    if (fileList.length === 0) return;
    if (loading || streaming) return;

    setLiveLog([]);
    setResult(null);

    if (selectedModel) {
      setStreaming(true);
      try {
        const response = await fetch("/analyze", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ files: fileList, theme, model: selectedModel }),
        });
        if (!response.ok) throw new Error(await response.text());

        const reader = response.body!.getReader();
        const decoder = new TextDecoder();
        let buffer = "";

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split("\n");
          buffer = lines.pop()!;

          for (const line of lines) {
            if (line.startsWith("data: ")) {
              const jsonStr = line.slice(6);
              try {
                const event = JSON.parse(jsonStr);
                if (event.type === "status") {
                  setLiveLog((prev) => [...prev, event.message]);
                } else if (event.type === "result") {
                  const res: AnalyzeResponse = event.result;
                  setResult(res);
                } else if (event.type === "error") {
                  alert(event.message);
                }
              } catch {
                // ignore malformed
              }
            }
          }
        }
      } catch (error: any) {
        console.error("Streaming error", error);
        alert("Erreur lors de l'analyse avec le modèle NPU.");
      } finally {
        setStreaming(false);
      }
      return;
    }

    // heuristique
    setLoading(true);
    try {
      const response = await fetch("/analyze", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ files: fileList, theme }),
      });
      const data: AnalyzeResponse = await response.json();
      setResult(data);
    } catch (error) {
      console.error(error);
      setResult(null);
    } finally {
      setLoading(false);
    }
  };

  const downloadScript = () => {
    if (!result?.powershellScript) return;
    const blob = new Blob([result.powershellScript], { type: "text/plain" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "rangement.ps1";
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <div style={{ padding: "1rem", fontFamily: "sans-serif" }}>
      <h2>🗂️ Rangement intelligent (via LLM sur NPU)</h2>

      <div style={{ marginBottom: "1rem" }}>
        <button onClick={handleSelectFolder}>📁 Choisir un dossier</button>
        {fileList.length > 0 && (
          <span style={{ marginLeft: "1rem" }}>{fileList.length} fichier(s) trouvé(s)</span>
        )}
      </div>

      <div style={{ marginBottom: "1rem" }}>
        <label htmlFor="model-select">Modèle LLM NPU : </label>
        <select
          id="model-select"
          value={selectedModel}
          onChange={(e) => setSelectedModel(e.target.value)}
        >
          <option value="">(Aucun, utiliser l'heuristique)</option>
          {models.map((m) => (
            <option key={m} value={m}>{m}</option>
          ))}
        </select>
      </div>

      <div style={{ marginBottom: "1rem" }}>
        <label htmlFor="theme">Thème souhaité : </label>
        <input
          id="theme"
          type="text"
          placeholder="ex: photos, documents, code..."
          value={theme}
          onChange={(e) => setTheme(e.target.value)}
          style={{ marginLeft: "0.5rem", width: "200px" }}
        />
        <button
          onClick={handleAnalyze}
          disabled={loading || streaming || fileList.length === 0}
          style={{ marginLeft: "1rem" }}
        >
          {streaming ? "Analyse en direct…" : loading ? "Analyse en cours…" : "🔍 Analyser"}
        </button>
      </div>

      {(streaming || liveLog.length > 0) && (
        <div
          style={{
            background: "#eee",
            padding: "0.5em",
            maxHeight: "150px",
            overflow: "auto",
            marginTop: "1rem",
            border: "1px solid #ccc",
          }}
        >
          {liveLog.map((msg, i) => (
            <div key={i}>{msg}</div>
          ))}
        </div>
      )}

      {result && (
        <div style={{ marginTop: "1.5rem" }}>
          <h3>Plan de rangement</h3>
          <ul>
            {Object.entries(result.plan).map(([folder, files]) => (
              <li key={folder}>
                <strong>{folder}</strong> ({files.length}) → {files.join(", ")}
              </li>
            ))}
          </ul>

          <h3>Résumés (quelques mots)</h3>
          <ul>
            {Object.entries(result.summary).map(([rel, summ]) => (
              <li key={rel}>{rel} → {summ}</li>
            ))}
          </ul>

          {Object.keys(result.duplicates).length > 0 && (
            <>
              <h3>Doublons détectés</h3>
              <ul>
                {Object.entries(result.duplicates).map(([keep, dups]) => (
                  <li key={keep}>
                    Conserver <strong>{keep}</strong>, supprimer {dups.join(", ")}
                  </li>
                ))}
              </ul>
            </>
          )}

          <h3>Script PowerShell</h3>
          <pre
            style={{
              background: "#f5f5f5",
              padding: "0.5rem",
              maxHeight: "300px",
              overflow: "auto",
            }}
          >
            {result.powershellScript}
          </pre>
          <button onClick={downloadScript} style={{ marginTop: "0.5rem" }}>
            ⬇️ Télécharger le script
          </button>
        </div>
      )}
    </div>
  );
};

export default Component;
