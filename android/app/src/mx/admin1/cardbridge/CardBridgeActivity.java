package mx.admin1.cardbridge;

import android.app.Activity;
import android.graphics.Color;
import android.nfc.NfcAdapter;
import android.nfc.Tag;
import android.nfc.tech.IsoDep;
import android.os.Build;
import android.os.Bundle;
import android.view.Gravity;
import android.widget.LinearLayout;
import android.widget.TextView;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import org.json.JSONObject;

public final class CardBridgeActivity extends Activity implements NfcAdapter.ReaderCallback {
    private static final int PORT = 5380;
    private final Object cardLock = new Object();
    private NfcAdapter adapter;
    private IsoDep currentCard;
    private ServerSocket server;
    private volatile boolean running;
    private TextView status;

    @Override
    public void onCreate(Bundle state) {
        super.onCreate(state);
        LinearLayout layout = new LinearLayout(this);
        layout.setOrientation(LinearLayout.VERTICAL);
        layout.setGravity(Gravity.CENTER);
        layout.setPadding(48, 48, 48, 48);
        layout.setBackgroundColor(Color.rgb(15, 23, 42));
        TextView title = new TextView(this);
        title.setText("SWIFT LEDGER\nCARD BRIDGE");
        title.setTextColor(Color.WHITE);
        title.setTextSize(28);
        title.setGravity(Gravity.CENTER);
        title.setPadding(0, 0, 0, 40);
        layout.addView(title);
        status = new TextView(this);
        status.setTextColor(Color.rgb(203, 213, 225));
        status.setTextSize(18);
        status.setGravity(Gravity.CENTER);
        layout.addView(status);
        setContentView(layout);
        adapter = NfcAdapter.getDefaultAdapter(this);
        startServer();
    }

    @Override
    public void onResume() {
        super.onResume();
        if (adapter != null) {
            int flags = NfcAdapter.FLAG_READER_NFC_A | NfcAdapter.FLAG_READER_NFC_B
                    | NfcAdapter.FLAG_READER_NFC_F | NfcAdapter.FLAG_READER_NFC_V
                    | NfcAdapter.FLAG_READER_SKIP_NDEF_CHECK;
            adapter.enableReaderMode(this, this, flags, null);
        }
        showStatus();
    }

    @Override
    public void onPause() {
        if (adapter != null) adapter.disableReaderMode(this);
        synchronized (cardLock) {
            closeCard();
        }
        super.onPause();
    }

    @Override
    public void onDestroy() {
        running = false;
        if (server != null) {
            try { server.close(); } catch (Exception ignored) { }
        }
        super.onDestroy();
    }

    @Override
    public void onTagDiscovered(Tag tag) {
        IsoDep next = IsoDep.get(tag);
        if (next == null) return;
        synchronized (cardLock) {
            closeCard();
            currentCard = next;
        }
        runOnUiThread(new Runnable() {
            @Override public void run() {
                status.setText("Tarjeta ISO-DEP detectada\nMantén la tarjeta sobre la SUNMI");
            }
        });
    }

    private void showStatus() {
        if (adapter == null) status.setText("NFC no disponible en este dispositivo");
        else if (!adapter.isEnabled()) status.setText("Activa NFC en la SUNMI");
        else status.setText("Lector listo\nAcerca una tarjeta Swift Ledger\nPuerto local 5380");
    }

    private void closeCard() {
        if (currentCard != null) {
            try { currentCard.close(); } catch (Exception ignored) { }
            currentCard = null;
        }
    }

    private JSONObject exchange(String hex) throws Exception {
        synchronized (cardLock) {
            if (currentCard == null) return error("NO_CARD", "Acerca una tarjeta a la SUNMI.");
            byte[] command = parseHex(hex);
            currentCard.setTimeout(6000);
            if (!currentCard.isConnected()) currentCard.connect();
            byte[] selected = currentCard.transceive(AppletAid.SELECT_AID);
            int selectSw = statusWord(selected);
            if (selectSw != 0x9000) return response(selectSw, new byte[0]);
            byte[] answer = currentCard.transceive(command);
            return response(statusWord(answer), payload(answer));
        }
    }

    private static byte[] parseHex(String value) {
        if (value == null || !value.matches("[0-9A-Fa-f]+") || value.length() < 10
                || value.length() > 512 || value.length() % 2 != 0) {
            throw new IllegalArgumentException("APDU inválido");
        }
        byte[] result = new byte[value.length() / 2];
        for (int i = 0; i < result.length; i++) {
            result[i] = (byte) Integer.parseInt(value.substring(i * 2, i * 2 + 2), 16);
        }
        return result;
    }

    private static int statusWord(byte[] response) {
        if (response == null || response.length < 2) return 0;
        return ((response[response.length - 2] & 0xFF) << 8)
                | (response[response.length - 1] & 0xFF);
    }

    private static byte[] payload(byte[] response) {
        if (response == null || response.length < 2) return new byte[0];
        byte[] data = new byte[response.length - 2];
        System.arraycopy(response, 0, data, 0, data.length);
        return data;
    }

    private static JSONObject response(int sw, byte[] data) throws Exception {
        JSONObject value = new JSONObject();
        value.put("sw", sw);
        value.put("data", hex(data));
        return value;
    }

    private static JSONObject error(String code, String message) throws Exception {
        JSONObject value = new JSONObject();
        value.put("error", code);
        value.put("message", message);
        return value;
    }

    private static String hex(byte[] data) {
        StringBuilder out = new StringBuilder();
        for (byte value : data) out.append(String.format("%02X", value & 0xFF));
        return out.toString();
    }

    private void startServer() {
        running = true;
        Thread thread = new Thread(new Runnable() {
            @Override public void run() { serve(); }
        }, "card-bridge-http");
        thread.setDaemon(true);
        thread.start();
    }

    private void serve() {
        try {
            server = new ServerSocket();
            server.bind(new InetSocketAddress(InetAddress.getByName("127.0.0.1"), PORT));
            while (running) {
                try {
                    Socket socket = server.accept();
                    handle(socket);
                } catch (Exception ignored) { }
            }
        } catch (Exception ignored) { }
    }

    private void handle(Socket socket) {
        try {
            socket.setSoTimeout(8000);
            InputStream input = socket.getInputStream();
            ByteArrayOutputStream headerBytes = new ByteArrayOutputStream();
            int matched = 0;
            while (matched < 4 && headerBytes.size() < 8192) {
                int value = input.read();
                if (value < 0) throw new IllegalArgumentException("solicitud incompleta");
                headerBytes.write(value);
                int expected = matched == 0 || matched == 2 ? 13 : 10;
                matched = value == expected ? matched + 1 : 0;
            }
            String headers = new String(headerBytes.toByteArray(), StandardCharsets.ISO_8859_1);
            String[] lines = headers.split("\\r\\n");
            String[] first = lines[0].split(" ");
            if (first.length < 2) throw new IllegalArgumentException("solicitud inválida");
            int length = 0;
            for (String line : lines) {
                if (line.toLowerCase().startsWith("content-length:")) {
                    length = Integer.parseInt(line.substring(line.indexOf(':') + 1).trim());
                }
            }
            if (length < 0 || length > 4096) throw new IllegalArgumentException("cuerpo inválido");
            byte[] body = new byte[length];
            int offset = 0;
            while (offset < length) {
                int count = input.read(body, offset, length - offset);
                if (count < 0) throw new IllegalArgumentException("cuerpo incompleto");
                offset += count;
            }
            JSONObject result;
            int httpStatus = 200;
            if ("GET".equals(first[0]) && "/health".equals(first[1])) {
                result = new JSONObject();
                result.put("ok", true);
                result.put("service", "swift-ledger-card-bridge");
                result.put("model", Build.MODEL);
                result.put("reader", adapter != null && adapter.isEnabled());
            } else if ("POST".equals(first[0]) && "/apdu".equals(first[1])) {
                JSONObject request = new JSONObject(new String(body, StandardCharsets.UTF_8));
                result = exchange(request.getString("apdu"));
            } else {
                httpStatus = 404;
                result = error("NOT_FOUND", "Ruta no encontrada");
            }
            send(socket, httpStatus, result);
        } catch (Exception error) {
            try {
                send(socket, 400, error("BAD_REQUEST", "Solicitud o tarjeta no válida"));
            } catch (Exception ignored) { }
        } finally {
            try { socket.close(); } catch (Exception ignored) { }
        }
    }

    private static void send(Socket socket, int status, JSONObject value) throws Exception {
        byte[] body = value.toString().getBytes(StandardCharsets.UTF_8);
        String headers = "HTTP/1.1 " + status + " " + (status == 200 ? "OK" : "ERROR")
                + "\r\nContent-Type: application/json; charset=utf-8"
                + "\r\nContent-Length: " + body.length
                + "\r\nCache-Control: no-store\r\nX-Content-Type-Options: nosniff"
                + "\r\nConnection: close\r\n\r\n";
        OutputStream output = socket.getOutputStream();
        output.write(headers.getBytes(StandardCharsets.ISO_8859_1));
        output.write(body);
        output.flush();
    }
}
