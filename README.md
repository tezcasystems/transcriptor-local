# 🎙️ Transcriptor Local

Graba y transcribe reuniones de Teams o presenciales en tu propia PC: gratis, sin límite y sin que el audio salga a internet.

**Transcriptor Local** es una app para Windows que graba tus reuniones y las convierte en texto, todo dentro de tu computadora.

- **Reuniones web (Teams, Meet, Zoom):** graba tu micrófono y el audio de la llamada por separado, y la transcripción distingue lo que dijiste **tú** de lo que dijeron los **participantes**. Puedes silenciar tu micrófono cuando no hables con un botón o con un atajo de teclado, desde cualquier ventana.
- **Reuniones presenciales:** graba con el micrófono de la laptop y nivela el volumen de las voces cercanas y lejanas.
- **100% local y gratuito:** la transcripción la hace Whisper en tu PC. No hay suscripciones, límites de uso ni envío de audio a internet.
- **Listo para tu IA:** copia la transcripción con una instrucción de resumen incluida y pégala en tu asistente favorito.
- **Historial:** busca en tus reuniones pasadas por título o por lo que se dijo. El audio se borra solo después de 30 días y las transcripciones se conservan siempre.
- **Extra:** convierte PDF, Word, Excel, PowerPoint, audio y video a texto.

## Instalación

Requisitos: Windows 10 u 11, ~3 GB libres e internet solo durante la instalación.

1. Descarga el proyecto (**Code → Download ZIP**) y descomprímelo, por ejemplo en `Documentos\transcriptor-local`.
2. Doble clic en **`instalar.bat`**. Instala Python 3.12 (si no lo tienes), las librerías, ffmpeg y el modelo de voz. Tarda unos minutos.
3. Doble clic en **`iniciar.bat`**. La app se abre en tu navegador en `http://127.0.0.1:7860`.

> Deja abierta (minimizada) la ventana negra mientras uses la app: si la cierras, la app se detiene.

## Uso

**Grabar una reunión**

1. Elige el tipo: **Reunión web** o **Presencial**.
2. Elige el micrófono y pulsa **Probar audio**.
3. Pulsa **Iniciar grabación**. Durante la reunión puedes pausar o silenciar tu micrófono.
4. Pulsa **Detener y transcribir**. La transcripción aparece sola al terminar (unos 18 minutos por cada hora de reunión, en segundo plano).
5. Usa **Copiar con instrucción de resumen** y pégalo en tu IA.

**Buenas prácticas**

- 🎧 En reuniones web usa audífonos; los de cable suenan mejor que los Bluetooth en llamadas.
- 🔇 Silenciarte en Teams **no** silencia la grabación: usa el botón *Silenciar* o el atajo que muestra la app (por ejemplo **Ctrl+Alt+Espacio**).
- 💻 En reuniones presenciales deja la laptop al centro de la mesa.
- 📢 **Avisa a los participantes que vas a grabar.**

## Dónde se guarda todo

En `Documentos\Reuniones`, una carpeta por reunión:

```
Reuniones\2026\2026-09-25_10-30-00_Junta semanal\
├── transcripcion.md
├── meeting.json
└── audio\  (mic.ogg, pc.ogg, reunion_completa.ogg)
```

El audio se borra automáticamente a los 30 días (configurable). Las transcripciones se conservan siempre.

## Privacidad

- La app solo es accesible desde tu propia PC (`127.0.0.1`).
- El audio y las transcripciones nunca salen de tu computadora.
- Única excepción opcional: en *Convertir archivos* puedes poner una API key de OpenAI para leer texto de imágenes; en ese caso esas imágenes se envían a OpenAI.

## Solución de problemas

| Problema | Qué hacer |
|---|---|
| «El micrófono no envía sonido» | Elige otro micrófono. Revisa *Configuración de Windows → Privacidad y seguridad → Micrófono → Permitir que las aplicaciones de escritorio accedan al micrófono*. |
| No se escucha a los participantes | Pulsa *Probar audio* mientras suena algo en la PC. |
| Algo más falló | En la pestaña **⚙️ Configuración** revisa el diagnóstico, y comparte el archivo `Documentos\Reuniones\logs\app.log` con quien te dé soporte. |

## Tecnología

Python · [Gradio](https://www.gradio.app/) · [faster-whisper](https://github.com/SYSTRAN/faster-whisper) · [ffmpeg](https://ffmpeg.org/) · [SoundCard](https://github.com/bastibe/SoundCard) · [MarkItDown](https://github.com/microsoft/markitdown)
