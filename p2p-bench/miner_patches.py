"""Trace lines in the miner for the measurement of empty blocks; the node stays the qualified one.

Every answer of the node to a template request, every job the miner starts with its transaction count,
every find by the kind of its job, and every submission with the time it waited for a slot and the time
the node took to take it. With -d the miner's log carries milliseconds, so the lines need no clock of
their own. Nothing else changes: the miner asks, mines and submits as before.
"""

from pathlib import Path

FEED = Path("crates/mining/src/node/feed.rs")
SOURCE = Path("bins/miner/src/source/node.rs")

PATCHES = [
    (FEED, """                Ok(template) if self.spent_again(&template) => self.empty_or(template).await,
""", """                Ok(template) if self.spent_again(&template) => {
                    tracing::debug!(
                        "trace spent_again answer_ms={} found={found}",
                        asked.elapsed().as_millis()
                    );
                    self.empty_or(template).await
                }
"""),
    (FEED, """                    tracing::debug!("the node answered in {} ms", asked.elapsed().as_millis());
""", """                    tracing::debug!(
                        "trace answer answer_ms={} transactions={} found={found}",
                        asked.elapsed().as_millis(),
                        template.body().transactions().len()
                    );
"""),
    (FEED, """                    tracing::debug!("the node asks for {} ms before new work", wait.as_millis());
""", """                    tracing::debug!("trace throttled wait_ms={}", wait.as_millis());
"""),
    (FEED, """                .is_some_and(|latest| latest.same_work(&template))
            {
                continue;
            }
""", """                .is_some_and(|latest| latest.same_work(&template))
            {
                tracing::debug!(
                    "trace same_work transactions={}",
                    template.body().transactions().len()
                );
                continue;
            }
"""),
    (FEED, """            Ok(empty) => empty,
""", """            Ok(empty) => {
                tracing::debug!("trace empty_work");
                empty
            }
"""),
    (SOURCE, """                let Some((_, template)) = recent.iter().find(|(serial, _)| *serial == solution.serial) else {
                    continue;
                };
""", """                let Some((_, template)) = recent.iter().find(|(serial, _)| *serial == solution.serial) else {
                    tracing::debug!("trace found unknown");
                    continue;
                };
"""),
    (SOURCE, """                        tracing::debug!("another block of job {} would repeat its transactions", short(&job.to_hex()));
""", """                        tracing::debug!("another block of job {} would repeat its transactions", short(&job.to_hex()));
                        tracing::debug!("trace found job {} repeat", short(&job.to_hex()));
"""),
    (SOURCE, """                    tracing::debug!("block of job {} found, its transactions are spent", short(&job.to_hex()));
                    carried.mark(job);
                }
""", """                    tracing::debug!("block of job {} found, its transactions are spent", short(&job.to_hex()));
                    tracing::debug!("trace found job {} filled", short(&job.to_hex()));
                    carried.mark(job);
                } else {
                    tracing::debug!("trace found job {} empty", short(&template.job_id().to_hex()));
                }
"""),
    (SOURCE, """                    dropped.note(&block);
""", """                    tracing::debug!("trace dropped block {block}");
                    dropped.note(&block);
"""),
    (SOURCE, """                recent.push_back((work.publish(Some(job)), template));
""", """                tracing::debug!(
                    "trace job {} transactions={}",
                    short(&template.job_id().to_hex()),
                    template.body().transactions().len()
                );
                recent.push_back((work.publish(Some(job)), template));
"""),
    (SOURCE, """                tokio::spawn(async move {
                    let permit = in_flight.acquire_owned().await;
                    let answer = submit(&node, &header, &template).await;
                    drop(permit);
                    waiting.fetch_sub(1, Ordering::AcqRel);
""", """                tokio::spawn(async move {
                    let queued = Instant::now();
                    let permit = in_flight.acquire_owned().await;
                    let sent = Instant::now();
                    let answer = submit(&node, &header, &template).await;
                    drop(permit);
                    waiting.fetch_sub(1, Ordering::AcqRel);
                    tracing::debug!(
                        "trace submitted block {block} transactions={} slot_ms={} answer_ms={} taken={}",
                        template.body().transactions().len(),
                        sent.duration_since(queued).as_millis(),
                        sent.elapsed().as_millis(),
                        matches!(answer, Ok(Submission::Accepted | Submission::Known))
                    );
"""),
]
