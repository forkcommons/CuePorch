export interface SlideProgram {
  id: string;
  page: number;
  pages: number;
  revision: number;
}

export async function readStudioImage(address: string, credential: string, signal?: AbortSignal) {
  const response = await fetch(address, {headers: {Authorization: `Bearer ${credential}`}, signal});
  if (!response.ok) throw new Error("A selected image or slide is unavailable.");
  const url = URL.createObjectURL(await response.blob());
  const image = new Image();
  image.src = url;
  try { await image.decode(); }
  catch { URL.revokeObjectURL(url); throw new Error("A selected image could not be decoded."); }
  return {image, url};
}
