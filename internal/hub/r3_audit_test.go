// Copyright 2026 Christian Haeussinger
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package hub

import (
	"context"
	"fmt"
	"net"
	"strconv"
	"strings"
	"sync"
	"testing"

	pubsub "cloud.google.com/go/pubsub/v2/apiv1"
	"cloud.google.com/go/pubsub/v2/apiv1/pubsubpb"
	"github.com/alicebob/miniredis/v2"
	"github.com/redis/go-redis/v9"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"google.golang.org/api/option"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"
)

func TestSpawnQueueName_AlwaysNonNegativeShard(t *testing.T) {
	for _, shards := range []int{1, 8, 16, 64} {
		for i := 0; i < 1000; i++ {
			sid := fmt.Sprintf("session-msb-check-%d", i)
			q := SpawnQueueName("runner", sid, shards)
			parts := strings.Split(q, ":")
			require.Len(t, parts, 4)
			shardIdx, err := strconv.Atoi(parts[3])
			require.NoError(t, err)
			assert.GreaterOrEqual(t, shardIdx, 0)
			assert.Less(t, shardIdx, shards)
		}
	}
}

func TestFlushQueues_NilAndGlobalTenantCounts(t *testing.T) {
	var nilSB *RedisSwitchboard
	_, err := nilSB.FlushQueues(context.Background())
	require.Error(t, err)

	mr := miniredis.RunT(t)
	client := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	defer client.Close()

	ctx := context.Background()
	require.NoError(t, client.RPush(ctx, "simulation:spawns:runner:0", "a").Err())
	require.NoError(t, client.RPush(ctx, "t:acme:simulation:spawns:runner:1", "b").Err())

	sb := &RedisSwitchboard{client: client}
	n, err := sb.FlushQueues(ctx)
	require.NoError(t, err)
	assert.Equal(t, 2, n)
}

func TestRedisSubscriptionStore_NilAndEmptyGuards(t *testing.T) {
	var nilStore *RedisSubscriptionStore
	ctx := context.Background()
	require.Error(t, nilStore.Subscribe(ctx, "sim", "sess"))
	require.Error(t, nilStore.Unsubscribe(ctx, "sim", "sess"))
	require.Error(t, nilStore.UnsubscribeAll(ctx, "sess"))
	_, err := nilStore.Lookup(ctx, "sim")
	require.Error(t, err)

	store, _ := newTestRedisSubscriptionStore(t)
	require.Error(t, store.Subscribe(ctx, "", "sess"))
	require.Error(t, store.Subscribe(ctx, "sim", ""))
	require.Error(t, store.Unsubscribe(ctx, "", "sess"))
	require.Error(t, store.Unsubscribe(ctx, "sim", ""))
	require.Error(t, store.UnsubscribeAll(ctx, ""))
	_, err = store.Lookup(ctx, "")
	require.Error(t, err)
}

func TestDispatchAndPoke_CancelledContextReturnsEarly(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()

	sb := &RedisSwitchboard{}
	require.ErrorIs(t, sb.DispatchToAgent(ctx, "runner", map[string]string{"k": "v"}), context.Canceled)
	require.ErrorIs(t, sb.PokeAgent(ctx, "runner", map[string]string{"k": "v"}), context.Canceled)
}

type fakePubSubSeekServer struct {
	pubsubpb.UnimplementedSubscriberServer
	mu   sync.Mutex
	seen []string
	fail map[string]error
}

func (f *fakePubSubSeekServer) Seek(_ context.Context, req *pubsubpb.SeekRequest) (*pubsubpb.SeekResponse, error) {
	f.mu.Lock()
	f.seen = append(f.seen, req.GetSubscription())
	f.mu.Unlock()
	if err, ok := f.fail[req.GetSubscription()]; ok {
		return nil, err
	}
	return &pubsubpb.SeekResponse{}, nil
}

func (f *fakePubSubSeekServer) snapshot() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := make([]string, len(f.seen))
	copy(out, f.seen)
	return out
}

func (f *fakePubSubSeekServer) reset() {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.seen = nil
}

func TestGCPPubSubDrainer_PathProjectValidationAndClose(t *testing.T) {
	lis, err := net.Listen("tcp", "127.0.0.1:0")
	require.NoError(t, err)
	defer lis.Close()

	fake := &fakePubSubSeekServer{
		fail: map[string]error{
			"projects/other-proj/subscriptions/broken-sub": status.Error(codes.NotFound, "missing"),
		},
	}
	grpcSrv := grpc.NewServer()
	pubsubpb.RegisterSubscriberServer(grpcSrv, fake)
	go func() { _ = grpcSrv.Serve(lis) }()
	defer grpcSrv.Stop()

	origFactory := newSubscriptionAdminClient
	newSubscriptionAdminClient = func(ctx context.Context) (*pubsub.SubscriptionAdminClient, error) {
		conn, dialErr := grpc.NewClient(lis.Addr().String(), grpc.WithTransportCredentials(insecure.NewCredentials()))
		if dialErr != nil {
			return nil, dialErr
		}
		return pubsub.NewSubscriptionAdminClient(ctx, option.WithGRPCConn(conn))
	}
	defer func() { newSubscriptionAdminClient = origFactory }()

	// Empty projectID + short sub name fails loudly, while full path still succeeds
	// and partial failure returns both drained count and error.
	d1 := NewGCPPubSubDrainer("", []string{"short-sub", "projects/other-proj/subscriptions/good-sub", "projects/other-proj/subscriptions/broken-sub"})
	count, drainErr := d1.Drain(context.Background())
	assert.Equal(t, 1, count)
	require.Error(t, drainErr)
	assert.Contains(t, drainErr.Error(), "requires non-empty projectID")
	assert.Equal(t, []string{
		"projects/other-proj/subscriptions/good-sub",
		"projects/other-proj/subscriptions/broken-sub",
	}, fake.snapshot())
	require.NoError(t, d1.Close())
	require.NoError(t, d1.Close())

	// Normal short + full path combination without errors.
	fake.reset()
	d2 := NewGCPPubSubDrainer("my-proj", []string{"short-sub", "projects/p2/subscriptions/full-sub", "  "})
	count2, err2 := d2.Drain(context.Background())
	require.NoError(t, err2)
	assert.Equal(t, 2, count2)
	assert.Equal(t, []string{
		"projects/my-proj/subscriptions/short-sub",
		"projects/p2/subscriptions/full-sub",
	}, fake.snapshot())
	require.NoError(t, d2.Close())

	var nilDrainer *GCPPubSubDrainer
	assert.NoError(t, nilDrainer.Close())
	n0, err0 := nilDrainer.Drain(context.Background())
	assert.Equal(t, 0, n0)
	assert.NoError(t, err0)
}
