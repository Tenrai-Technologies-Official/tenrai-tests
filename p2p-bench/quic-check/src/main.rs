//! Throughput of one QUIC stream, for comparing Cubic and BBR through wan-proxy.
//!
//!   quic-check server PORT
//!   quic-check client SERVER_ADDR LOCAL_PORT SECONDS cubic|bbr
//!
//! The client sends with the node's windows (2 MiB per stream, 8 MiB per connection) and prints the
//! rate each second; it binds LOCAL_PORT so wan-proxy knows the sender.

use quinn::crypto::rustls::QuicClientConfig;
use quinn::{ClientConfig, Endpoint, ServerConfig, TransportConfig, congestion};
use rustls::client::danger::{HandshakeSignatureValid, ServerCertVerified, ServerCertVerifier};
use rustls::pki_types::{CertificateDer, PrivatePkcs8KeyDer, ServerName, UnixTime};
use rustls::{DigitallySignedStruct, SignatureScheme};
use std::net::SocketAddr;
use std::sync::Arc;
use std::time::{Duration, Instant};

fn transport(bbr: bool) -> Arc<TransportConfig> {
    let mut transport = TransportConfig::default();
    transport.stream_receive_window((2_u32 * 1024 * 1024).into());
    transport.receive_window((8_u32 * 1024 * 1024).into());
    if bbr {
        transport.congestion_controller_factory(Arc::new(congestion::BbrConfig::default()));
    }
    Arc::new(transport)
}

#[derive(Debug)]
struct AnyCertificate(Arc<rustls::crypto::CryptoProvider>);

impl ServerCertVerifier for AnyCertificate {
    fn verify_server_cert(
        &self,
        _: &CertificateDer<'_>,
        _: &[CertificateDer<'_>],
        _: &ServerName<'_>,
        _: &[u8],
        _: UnixTime,
    ) -> Result<ServerCertVerified, rustls::Error> {
        Ok(ServerCertVerified::assertion())
    }

    fn verify_tls12_signature(
        &self,
        _: &[u8],
        _: &CertificateDer<'_>,
        _: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, rustls::Error> {
        Ok(HandshakeSignatureValid::assertion())
    }

    fn verify_tls13_signature(
        &self,
        _: &[u8],
        _: &CertificateDer<'_>,
        _: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, rustls::Error> {
        Ok(HandshakeSignatureValid::assertion())
    }

    fn supported_verify_schemes(&self) -> Vec<SignatureScheme> {
        self.0.signature_verification_algorithms.supported_schemes()
    }
}

async fn server(port: u16) {
    let certificate = rcgen::generate_simple_self_signed(vec!["localhost".to_owned()]).unwrap();
    let key = PrivatePkcs8KeyDer::from(certificate.key_pair.serialize_der());
    let mut config =
        ServerConfig::with_single_cert(vec![certificate.cert.der().clone()], key.into()).unwrap();
    config.transport = transport(false);
    let endpoint = Endpoint::server(config, SocketAddr::from(([127, 0, 0, 1], port))).unwrap();
    while let Some(incoming) = endpoint.accept().await {
        tokio::spawn(async move {
            let Ok(connection) = incoming.await else { return };
            while let Ok((_, mut receive)) = connection.accept_bi().await {
                let mut buffer = vec![0_u8; 64 * 1024];
                while let Ok(Some(_)) = receive.read(&mut buffer).await {}
            }
        });
    }
}

async fn client(server: SocketAddr, local: u16, seconds: u64, bbr: bool) {
    let provider = Arc::new(rustls::crypto::ring::default_provider());
    let crypto = rustls::ClientConfig::builder_with_provider(provider.clone())
        .with_protocol_versions(&[&rustls::version::TLS13])
        .unwrap()
        .dangerous()
        .with_custom_certificate_verifier(Arc::new(AnyCertificate(provider)))
        .with_no_client_auth();
    let mut config = ClientConfig::new(Arc::new(QuicClientConfig::try_from(crypto).unwrap()));
    config.transport_config(transport(bbr));
    let mut endpoint = Endpoint::client(SocketAddr::from(([127, 0, 0, 1], local))).unwrap();
    endpoint.set_default_client_config(config);
    let connection = endpoint.connect(server, "localhost").unwrap().await.unwrap();
    let (mut send, _) = connection.open_bi().await.unwrap();
    let chunk = vec![7_u8; 64 * 1024];
    let started = Instant::now();
    let mut next = started + Duration::from_secs(1);
    let mut sent_at_mark = 0_u64;
    let mut sent = 0_u64;
    while started.elapsed() < Duration::from_secs(seconds) {
        send.write_all(&chunk).await.unwrap();
        sent += chunk.len() as u64;
        if Instant::now() >= next {
            let stats = connection.stats();
            println!(
                "t={:>3}s {:>7.0} KB/s, rtt {:>4} ms, cwnd {:>8} B, lost {}",
                started.elapsed().as_secs(),
                (sent - sent_at_mark) as f64 / 1024.0,
                stats.path.rtt.as_millis(),
                stats.path.cwnd,
                stats.path.lost_packets
            );
            sent_at_mark = sent;
            next += Duration::from_secs(1);
        }
    }
    println!("mean {:.0} KB/s", sent as f64 / 1024.0 / started.elapsed().as_secs_f64());
}

#[tokio::main]
async fn main() {
    let args: Vec<String> = std::env::args().collect();
    match args.get(1).map(String::as_str) {
        Some("server") => server(args[2].parse().unwrap()).await,
        Some("client") => {
            client(args[2].parse().unwrap(), args[3].parse().unwrap(), args[4].parse().unwrap(), args[5] == "bbr")
                .await
        }
        _ => eprintln!("quic-check server PORT | quic-check client SERVER_ADDR LOCAL_PORT SECONDS cubic|bbr"),
    }
}
