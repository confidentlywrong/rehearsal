// Copyright 2026 Google LLC
// Modifications copyright 2026 Christian Haeussinger
// Changed for The Rehearsal: adapted simulation runtime, agents, and infrastructure for executive leadership rehearsal.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package hub

import (
	"context"
	"errors"
	"fmt"
	"log"
	"strings"
	"sync"

	pubsub "cloud.google.com/go/pubsub/v2/apiv1"
	"cloud.google.com/go/pubsub/v2/apiv1/pubsubpb"
	"google.golang.org/protobuf/types/known/timestamppb"
)

// PubSubDrainer drains backlogged messages from PubSub subscriptions
// by seeking them to "now". Used during environment reset on GCP.
type PubSubDrainer interface {
	Drain(ctx context.Context) (int, error)
}

// NoOpDrainer is used when PubSub drain is not configured (local dev / emulator).
type NoOpDrainer struct{}

// Drain is a no-op, returning 0 drained subscriptions.
func (d *NoOpDrainer) Drain(ctx context.Context) (int, error) {
	return 0, nil
}

// GCPPubSubDrainer seeks configured subscriptions to the current time,
// effectively discarding all backlogged messages.
type GCPPubSubDrainer struct {
	projectID     string
	subscriptions []string
	mu            sync.Mutex
	client        *pubsub.SubscriptionAdminClient
}

// NewGCPPubSubDrainer creates a drainer for the given project and subscriptions.
func NewGCPPubSubDrainer(projectID string, subscriptions []string) *GCPPubSubDrainer {
	cleaned := make([]string, 0, len(subscriptions))
	for _, s := range subscriptions {
		if trimmed := strings.TrimSpace(s); trimmed != "" {
			cleaned = append(cleaned, trimmed)
		}
	}
	return &GCPPubSubDrainer{
		projectID:     strings.TrimSpace(projectID),
		subscriptions: cleaned,
	}
}

// Close releases the underlying PubSub admin client if initialized.
func (d *GCPPubSubDrainer) Close() error {
	if d == nil {
		return nil
	}
	d.mu.Lock()
	client := d.client
	d.client = nil
	d.mu.Unlock()
	if client != nil {
		return client.Close()
	}
	return nil
}

// newSubscriptionAdminClient is the function used to create PubSub admin clients.
// It is a variable to allow replacement in tests.
var newSubscriptionAdminClient = func(ctx context.Context) (*pubsub.SubscriptionAdminClient, error) {
	return pubsub.NewSubscriptionAdminClient(ctx)
}

// Drain seeks all configured subscriptions to "now", discarding backlogged
// messages. Returns the number of subscriptions successfully drained.
func (d *GCPPubSubDrainer) Drain(ctx context.Context) (int, error) {
	if d == nil || len(d.subscriptions) == 0 {
		return 0, nil
	}

	d.mu.Lock()
	if d.client == nil {
		client, err := newSubscriptionAdminClient(ctx)
		if err != nil {
			d.mu.Unlock()
			return 0, fmt.Errorf("creating pubsub subscriber client: %w", err)
		}
		d.client = client
	}
	client := d.client
	d.mu.Unlock()

	drained := 0
	var errs []error
	for _, subName := range d.subscriptions {
		if ctx != nil {
			if err := ctx.Err(); err != nil {
				errs = append(errs, err)
				break
			}
		}
		var subPath string
		if strings.HasPrefix(subName, "projects/") {
			subPath = subName
		} else {
			if d.projectID == "" {
				err := fmt.Errorf("pubsub subscription %q requires non-empty projectID", subName)
				log.Printf("PubSubDrainer: %v", err)
				errs = append(errs, err)
				continue
			}
			subPath = fmt.Sprintf("projects/%s/subscriptions/%s", d.projectID, subName)
		}
		_, err := client.Seek(ctx, &pubsubpb.SeekRequest{
			Subscription: subPath,
			Target:       &pubsubpb.SeekRequest_Time{Time: timestamppb.Now()},
		})
		if err != nil {
			log.Printf("PubSubDrainer: failed to seek %s: %v", subName, err)
			errs = append(errs, fmt.Errorf("%s: %w", subPath, err))
			continue
		}
		log.Printf("PubSubDrainer: drained subscription %s", subName)
		drained++
	}

	if len(errs) > 0 {
		return drained, errors.Join(errs...)
	}
	return drained, nil
}
