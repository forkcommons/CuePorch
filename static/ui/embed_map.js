export async function saveEmbedMap(response) {
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement("a");
    link.href = url;
    link.download = "cueporch-embed-map.csv";
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
}
