package com.example.ytplaylistmanager

import android.annotation.SuppressLint
import android.content.Context
import android.os.Bundle
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.TextFieldValue
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.viewinterop.AndroidView
import com.example.ytplaylistmanager.theme.YTPlaylistManagerTheme

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        setContent {
            YTPlaylistManagerTheme {
                Surface(
                    modifier = Modifier.fillMaxSize(),
                    color = Color(0xFF0C0C14)
                ) {
                    Box(
                        modifier = Modifier
                            .fillMaxSize()
                            .statusBarsPadding()
                    ) {
                        AppContent()
                    }
                }
            }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun AppContent() {
    val context = LocalContext.current
    val sharedPrefs = remember { context.getSharedPreferences("yt_agent_prefs", Context.MODE_PRIVATE) }
    
    val defaultUrl = "https://clock-patriarch-stellar.ngrok-free.dev/"
    var savedUrl = sharedPrefs.getString("server_url", defaultUrl) ?: defaultUrl
    if (savedUrl.contains("trycloudflare.com") || savedUrl.contains("limiting-tin-coordinated-reduces")) {
        savedUrl = defaultUrl
        sharedPrefs.edit().putString("server_url", defaultUrl).apply()
    }
    var serverUrl by remember { mutableStateOf(savedUrl) }
    var isConfigured by remember { mutableStateOf(sharedPrefs.getBoolean("is_configured", true)) } // Default to true so it loads the URL first
    
    var showConfigScreen by remember { mutableStateOf(!isConfigured) }

    if (showConfigScreen) {
        ConfigScreen(
            currentUrl = serverUrl,
            onSave = { newUrl ->
                serverUrl = newUrl
                sharedPrefs.edit()
                    .putString("server_url", newUrl)
                    .putBoolean("is_configured", true)
                    .apply()
                showConfigScreen = false
            }
        )
    } else {
        DashboardWebView(
            url = serverUrl,
            onOpenSettings = {
                showConfigScreen = true
            }
        )
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ConfigScreen(currentUrl: String, onSave: (String) -> Unit) {
    var urlInput by remember { mutableStateOf(TextFieldValue(currentUrl)) }

    Box(
        modifier = Modifier
            .fillMaxSize()
            .background(
                Brush.verticalGradient(
                    colors = listOf(Color(0xFF1E1E38), Color(0xFF0C0C14))
                )
            ),
        contentAlignment = Alignment.Center
    ) {
        Card(
            modifier = Modifier
                .fillMaxWidth(0.9f)
                .padding(16.dp),
            colors = CardDefaults.cardColors(containerColor = Color(0x1FFFFFFF)),
            shape = RoundedCornerShape(24.dp),
            border = CardDefaults.outlinedCardBorder()
        ) {
            Column(
                modifier = Modifier
                    .padding(24.dp)
                    .fillMaxWidth(),
                horizontalAlignment = Alignment.CenterHorizontally,
                verticalArrangement = Arrangement.spacedBy(16.dp)
            ) {
                // Header
                Text(
                    text = "YT Playlist Manager",
                    fontSize = 24.sp,
                    fontWeight = FontWeight.Bold,
                    color = Color.White,
                    textAlign = TextAlign.Center
                )
                
                Text(
                    text = "Enter your YouTube Playlist Agent server endpoint below.",
                    fontSize = 14.sp,
                    color = Color(0xFFA5B4FC),
                    textAlign = TextAlign.Center
                )

                // Input
                OutlinedTextField(
                    value = urlInput,
                    onValueChange = { urlInput = it },
                    label = { Text("Server URL", color = Color(0xFFA5B4FC)) },
                    modifier = Modifier.fillMaxWidth(),
                    colors = OutlinedTextFieldDefaults.colors(
                        focusedTextColor = Color.White,
                        unfocusedTextColor = Color.White,
                        focusedContainerColor = Color(0x0FFFFFFF),
                        unfocusedContainerColor = Color(0x0FFFFFFF),
                        focusedBorderColor = Color(0xFF6366F1),
                        unfocusedBorderColor = Color(0x33FFFFFF)
                    ),
                    singleLine = true
                )

                Spacer(modifier = Modifier.height(8.dp))

                // Save button
                Button(
                    onClick = { onSave(urlInput.text.trim()) },
                    modifier = Modifier.fillMaxWidth(),
                    colors = ButtonDefaults.buttonColors(containerColor = Color(0xFF6366F1)),
                    shape = RoundedCornerShape(12.dp)
                ) {
                    Text("Connect Dashboard", fontWeight = FontWeight.SemiBold, color = Color.White)
                }
            }
        }
    }
}

@SuppressLint("SetJavaScriptEnabled")
@Composable
fun DashboardWebView(url: String, onOpenSettings: () -> Unit) {
    var webViewInstance by remember { mutableStateOf<WebView?>(null) }
    var isLoading by remember { mutableStateOf(true) }
    var hasError by remember { mutableStateOf(false) }
    var errorMessage by remember { mutableStateOf("") }

    Box(modifier = Modifier.fillMaxSize().background(Color(0xFF0C0C14))) {
        AndroidView(
            factory = { context ->
                WebView(context).apply {
                    setBackgroundColor(android.graphics.Color.TRANSPARENT)
                    webViewClient = object : WebViewClient() {
                        override fun onPageStarted(view: WebView?, url: String?, favicon: android.graphics.Bitmap?) {
                            isLoading = true
                            hasError = false
                        }

                        override fun onPageFinished(view: WebView?, url: String?) {
                            isLoading = false
                        }

                        override fun onReceivedError(
                            view: WebView?,
                            request: WebResourceRequest?,
                            error: WebResourceError?
                        ) {
                            // Only care about main frame errors
                            if (request?.isForMainFrame == true) {
                                hasError = true
                                errorMessage = error?.description?.toString() ?: "Failed to load dashboard"
                                isLoading = false
                            }
                        }
                    }
                    settings.apply {
                        javaScriptEnabled = true
                        domStorageEnabled = true
                        databaseEnabled = true
                        loadWithOverviewMode = false
                        useWideViewPort = false
                        cacheMode = android.webkit.WebSettings.LOAD_NO_CACHE
                        userAgentString = "YTPlaylistManagerAndroid/1.0"
                    }
                    clearCache(true)
                    val cacheBustedUrl = if (url.contains("?")) "$url&t=${System.currentTimeMillis()}" else "$url?t=${System.currentTimeMillis()}"
                    loadUrl(cacheBustedUrl, mapOf("ngrok-skip-browser-warning" to "true"))
                    webViewInstance = this
                }
            },
            update = { webView ->
                // Ensure it loads correct URL if changed
                val cacheBustedUrl = if (url.contains("?")) "$url&t=${System.currentTimeMillis()}" else "$url?t=${System.currentTimeMillis()}"
                if (webView.url != null && !webView.url!!.startsWith(url)) {
                    webView.loadUrl(cacheBustedUrl, mapOf("ngrok-skip-browser-warning" to "true"))
                }
            },
            modifier = Modifier.fillMaxSize()
        )

        // Overlay loading indicator
        AnimatedVisibility(
            visible = isLoading,
            enter = fadeIn(),
            exit = fadeOut()
        ) {
            Box(
                modifier = Modifier
                    .fillMaxSize()
                    .background(Color(0x990C0C14)),
                contentAlignment = Alignment.Center
            ) {
                CircularProgressIndicator(color = Color(0xFF6366F1))
            }
        }

        // Sleek Error Screen Overlay
        AnimatedVisibility(
            visible = hasError,
            enter = fadeIn(),
            exit = fadeOut()
        ) {
            Box(
                modifier = Modifier
                    .fillMaxSize()
                    .background(Color(0xFF0C0C14))
                    .padding(24.dp),
                contentAlignment = Alignment.Center
            ) {
                Column(
                    horizontalAlignment = Alignment.CenterHorizontally,
                    verticalArrangement = Arrangement.spacedBy(16.dp),
                    modifier = Modifier.fillMaxWidth()
                ) {
                    Text(
                        text = "Dashboard Offline",
                        fontSize = 22.sp,
                        fontWeight = FontWeight.Bold,
                        color = Color.White
                    )
                    Text(
                        text = "Could not connect to the server. Please verify the URL and ensure your local Python backend is running.\n\nError: $errorMessage",
                        fontSize = 14.sp,
                        color = Color(0xFFFDA4AF),
                        textAlign = TextAlign.Center
                    )
                    Row(
                        horizontalArrangement = Arrangement.spacedBy(12.dp)
                    ) {
                        Button(
                            onClick = { webViewInstance?.reload() },
                            colors = ButtonDefaults.buttonColors(containerColor = Color(0xFF6366F1))
                        ) {
                            Icon(Icons.Default.Refresh, contentDescription = "Retry", modifier = Modifier.size(16.dp))
                            Spacer(modifier = Modifier.width(8.dp))
                            Text("Retry")
                        }
                        Button(
                            onClick = onOpenSettings,
                            colors = ButtonDefaults.buttonColors(containerColor = Color(0x33FFFFFF))
                        ) {
                            Icon(Icons.Default.Settings, contentDescription = "Configure")
                            Spacer(modifier = Modifier.width(8.dp))
                            Text("Edit URL")
                        }
                    }
                }
            }
        }

        // Floating Action Button to configure settings/URL overlay when online
        if (!hasError && !isLoading) {
            Box(
                modifier = Modifier
                    .fillMaxSize()
                    .padding(16.dp),
                contentAlignment = Alignment.BottomEnd
            ) {
                FloatingActionButton(
                    onClick = onOpenSettings,
                    containerColor = Color(0xFF6366F1),
                    contentColor = Color.White,
                    modifier = Modifier.size(56.dp)
                ) {
                    Icon(Icons.Default.Settings, contentDescription = "Settings")
                }
            }
        }
    }
}
